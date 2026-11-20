from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flamingo_bot.config import load_model_config
from flamingo_bot.errors import ProviderError
from flamingo_bot.planning import SearchPlan
from flamingo_bot.providers.azure import AzureModelProvider


class Client:
    def __init__(self, response: Any) -> None:
        self.responses = SimpleNamespace(create=AsyncMock(return_value=response))
        self.options: dict[str, Any] = {}

    def with_options(self, **kwargs: Any) -> "Client":
        self.options = kwargs
        return self


def provider() -> AzureModelProvider:
    result = object.__new__(AzureModelProvider)
    result.config = load_model_config(Path("openai.yaml"))
    return result


@pytest.mark.asyncio
async def test_planner_and_reranker_use_independent_strict_json_roles() -> None:
    models = provider()
    plan_client = Client(
        SimpleNamespace(
            status="completed", output_text=SearchPlan.fallback("Question").model_dump_json()
        )
    )
    rank_client = Client(SimpleNamespace(status="completed", output_text='{"selected_ids":["C1"]}'))
    models.condense_client = plan_client  # type: ignore[assignment]
    models.rerank_client = rank_client  # type: ignore[assignment]
    await models.plan_search(
        instructions="Rules", prompt="Question", safety_identifier="hashed-visitor"
    )
    await models.select_passages(
        instructions="Rules", prompt="Passages", safety_identifier="hashed-visitor"
    )
    for client, expected in ((plan_client, "gpt-6-luna"), (rank_client, "gpt-5.6-luna")):
        call = client.responses.create.call_args.kwargs
        assert call["model"] == expected
        assert call["reasoning"] == {"effort": "none"}
        assert call["store"] is False
        assert call["safety_identifier"] == "hashed-visitor"
        assert call["text"]["format"]["strict"] is True
        assert client.options == {"max_retries": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,text", [("incomplete", "private failure details"), ("completed", "not JSON")]
)
async def test_invalid_planner_output_is_not_accepted_or_exposed(status: str, text: str) -> None:
    models = provider()
    models.condense_client = Client(SimpleNamespace(status=status, output_text=text))  # type: ignore[assignment]
    with pytest.raises(ProviderError) as failure:
        await models.plan_search(instructions="Rules", prompt="Question", safety_identifier=None)
    assert text not in str(failure.value)


class Stream:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.closed = False

    async def __aenter__(self) -> "Stream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def __aiter__(self) -> Any:
        for event in self.events:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete", "missing"])
async def test_sol_stream_requires_completion_and_closes_on_failure(terminal: str) -> None:
    models = provider()
    events = [SimpleNamespace(type="response.output_text.delta", delta="Answer [S1].")]
    if terminal != "missing":
        events.append(SimpleNamespace(type=terminal))
    stream = Stream(events)
    client = Client(stream)
    models.chat_client = client  # type: ignore[assignment]

    async def consume() -> str:
        return "".join(
            [
                part
                async for part in models.stream_answer(
                    instructions="Rules", prompt="Evidence", safety_identifier=None
                )
            ]
        )

    if terminal == "response.completed":
        assert await consume() == "Answer [S1]."
    else:
        with pytest.raises(ProviderError):
            await consume()
    assert stream.closed
    assert client.responses.create.call_args.kwargs["model"] == "gpt-6.1-sol"
    assert client.responses.create.call_args.kwargs["reasoning"] == {"effort": "low"}



@pytest.mark.asyncio
async def test_only_connections_that_never_opened_are_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from openai import APIConnectionError, APITimeoutError

    from flamingo_bot.providers import azure as azure_module

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(azure_module.asyncio, "sleep", no_sleep)
    request = httpx.Request("POST", "https://example.invalid/openai")

    def failure(cause: Exception) -> APIConnectionError:
        try:
            raise APIConnectionError(request=request) from cause
        except APIConnectionError as exc:
            return exc

    attempts: list[str] = []

    async def handshake_dropped_twice() -> str:
        attempts.append("call")
        if len(attempts) < 3:
            raise failure(httpx.ConnectError("TLS handshake dropped"))
        return "ok"

    assert await azure_module.with_connect_retry(handshake_dropped_twice) == "ok"
    assert len(attempts) == 3

    async def always_down() -> str:
        attempts.append("down")
        raise failure(httpx.ConnectError("unreachable"))

    attempts.clear()
    with pytest.raises(APIConnectionError):
        await azure_module.with_connect_retry(always_down)
    assert len(attempts) == len(azure_module.CONNECT_RETRY_DELAYS) + 1

    async def dropped_after_sending() -> str:
        attempts.append("sent")
        raise failure(httpx.RemoteProtocolError("server disconnected"))

    attempts.clear()
    with pytest.raises(APIConnectionError):
        await azure_module.with_connect_retry(dropped_after_sending)
    assert attempts == ["sent"]  # Azure may have received it: never sent twice

    async def slow() -> str:
        attempts.append("timeout")
        raise APITimeoutError(request=request)

    attempts.clear()
    with pytest.raises(APITimeoutError):
        await azure_module.with_connect_retry(slow)
    assert attempts == ["timeout"]


class Malformed:
    status = "completed"

    @property
    def output_text(self) -> str:
        raise TypeError("content list is null")


@pytest.mark.asyncio
async def test_malformed_structured_responses_become_provider_errors() -> None:
    models = provider()
    models.condense_client = Client(Malformed())  # type: ignore[assignment]
    with pytest.raises(ProviderError, match="malformed"):
        await models.classify_scope(instructions="Rules", prompt="Question", safety_identifier=None)


class BrokenStream(Stream):
    def __init__(self, failure: Exception) -> None:
        super().__init__([])
        self.failure = failure

    async def __aiter__(self) -> Any:
        yield SimpleNamespace(type="response.output_text.delta", delta="Partial answer")
        raise self.failure


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["transport", "delta"])
async def test_stream_failures_after_text_become_provider_errors(bad: str) -> None:
    import httpx

    models = provider()
    if bad == "transport":
        stream: Stream = BrokenStream(httpx.ReadTimeout("read timed out"))
    else:
        stream = Stream([SimpleNamespace(type="response.output_text.delta", delta=["not", "text"])])
    models.chat_client = Client(stream)  # type: ignore[assignment]
    with pytest.raises(ProviderError):
        async for _ in models.stream_answer(
            instructions="Rules", prompt="Evidence", safety_identifier=None
        ):
            pass
    assert stream.closed


def message(text: str) -> Any:
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


@pytest.mark.asyncio
async def test_a_reply_with_one_corrupted_and_one_clean_message_uses_the_clean_one() -> None:
    plan = SearchPlan.fallback("Paketa e Maleve").model_dump_json()
    corrupted = plan[:60] + '"]}\n","recent":false}<lemma> siulittaas'
    models = provider()
    models.condense_client = Client(  # type: ignore[assignment]
        SimpleNamespace(
            status="completed",
            output_text=corrupted + plan,  # what the SDK joins from the two messages
            output=[message(corrupted), message(plan)],
        )
    )
    result = await models.plan_search(instructions="Rules", prompt="Q", safety_identifier=None)
    assert result.queries == ["Paketa e Maleve"]

    models.condense_client = Client(  # type: ignore[assignment]
        SimpleNamespace(status="completed", output_text=corrupted, output=[message(corrupted)])
    )
    with pytest.raises(ProviderError):
        await models.plan_search(instructions="Rules", prompt="Q", safety_identifier=None)

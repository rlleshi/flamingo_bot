"""Azure-hosted OpenAI model access through the official Python SDK."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import httpx
from openai import APIConnectionError, APIError, APITimeoutError, AsyncAzureOpenAI
from pydantic import BaseModel, ValidationError

from flamingo_bot.config import ChatModelConfig, ModelConfig, Settings
from flamingo_bot.errors import ProviderError
from flamingo_bot.planning import (
    BroadSelection,
    FollowUpQueries,
    PassageSelection,
    QueryExpansion,
    QuestionScope,
    SearchPlan,
)

#: Pauses, before jitter, between attempts when a connection fails (timeouts excluded):
#: at most about six seconds in all, and only when no request reached the model.
CONNECT_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)


def first_valid[ModelT: BaseModel](
    schema: type[ModelT], candidates: Sequence[str], description: str
) -> ModelT:
    """The first candidate that validates in full against the strict schema."""
    for text in candidates:
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            continue
    raise ProviderError(f"Azure returned an invalid {description}")


def _never_sent(exc: APIConnectionError) -> bool:
    """True only when the connection could not be opened (for example a TLS handshake that
    was dropped), so no request reached Azure and a retry cannot duplicate one."""
    return not isinstance(exc, APITimeoutError) and isinstance(exc.__cause__, httpx.ConnectError)


async def with_connect_retry[ResultT](call: Callable[[], Awaitable[ResultT]]) -> ResultT:
    """Retry a model call whose connection could not be opened. Failures after the request
    was sent, timeouts and HTTP errors are not retried, so nothing runs twice and the
    call's own deadline still bounds each attempt."""
    for delay in CONNECT_RETRY_DELAYS:
        try:
            return await call()
        except APIConnectionError as exc:
            if not _never_sent(exc):
                raise
            await asyncio.sleep(delay * random.uniform(1.0, 1.5))
    return await call()


class AzureModelProvider:
    def __init__(self, settings: Settings, config: ModelConfig) -> None:
        endpoint, api_key = settings.require_azure_credentials()
        self.config = config
        self.chat_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=config.chat.api_version,
            timeout=config.chat.timeout_seconds,
            max_retries=0,
        )
        self.condense_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=config.condense.api_version,
            timeout=config.condense.timeout_seconds,
            max_retries=2,
        )
        self.embedding_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=config.embedding.api_version,
            timeout=config.embedding.timeout_seconds,
            max_retries=2,
        )
        self.rerank_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=config.rerank.api_version,
            timeout=config.rerank.timeout_seconds,
            max_retries=0,
        )

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings: list[list[float]] = []
        batch_size = self.config.embedding.batch_size
        try:
            for index in range(0, len(texts), batch_size):
                batch = list(texts[index : index + batch_size])
                response = await self.embedding_client.embeddings.create(
                    model=self.config.embedding.deployment,
                    input=batch,
                    dimensions=self.config.embedding.dimensions,
                    encoding_format="float",
                )
                ordered = sorted(response.data, key=lambda item: item.index)
                vectors = [list(item.embedding) for item in ordered]
                if len(vectors) != len(batch):
                    raise ProviderError("Azure returned an unexpected embedding count")
                if any(len(vector) != self.config.embedding.dimensions for vector in vectors):
                    raise ProviderError("Azure returned an unexpected embedding dimension")
                embeddings.extend(vectors)
        except APIError as exc:
            raise ProviderError("Azure embedding request failed") from exc
        return embeddings

    async def _structured_completion(
        self,
        *,
        client: AsyncAzureOpenAI,
        config: ChatModelConfig,
        schema: type[BaseModel],
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> list[str]:
        """Use strict JSON without retaining visitor input or logging provider payloads.

        Returns the reply's text candidates: the whole output text, then each output message
        on its own. The model occasionally sends two messages, one of them corrupted, and the
        SDK's joined output text then fails to parse; callers keep the first candidate that
        validates in full.
        """
        try:
            response = await with_connect_retry(
                lambda: client.with_options(max_retries=0).responses.create(
                    model=config.deployment,
                    instructions=instructions,
                    input=prompt,
                    reasoning={"effort": config.reasoning_effort},
                    text={
                        "verbosity": config.text_verbosity,
                        "format": {
                            "type": "json_schema",
                            "name": schema.model_config.get("title") or schema.__name__,
                            "schema": schema.model_json_schema(),
                            "strict": True,
                        },
                    },
                    max_output_tokens=config.max_output_tokens,
                    safety_identifier=safety_identifier,
                    store=False,
                )
            )
        except APIError as exc:
            raise ProviderError("Azure structured retrieval call failed") from exc
        try:
            status = response.status
            candidates = [response.output_text or ""]
            for item in getattr(response, "output", None) or []:
                for content in getattr(item, "content", None) or []:
                    text = getattr(content, "text", None)
                    if isinstance(text, str) and text not in candidates:
                        candidates.append(text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderError("Azure structured retrieval response was malformed") from exc
        if status != "completed":
            raise ProviderError("Azure structured retrieval response was incomplete")
        return candidates

    async def plan_search(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> SearchPlan:
        candidates = await self._structured_completion(
            client=self.condense_client,
            config=self.config.condense,
            schema=SearchPlan,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
        )
        return first_valid(SearchPlan, candidates, "retrieval plan")

    async def _condensed[ModelT: BaseModel](
        self,
        schema: type[ModelT],
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
        description: str,
    ) -> ModelT:
        candidates = await self._structured_completion(
            client=self.condense_client,
            config=self.config.condense,
            schema=schema,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
        )
        return first_valid(schema, candidates, description)

    async def classify_scope(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> QuestionScope:
        return await self._condensed(
            QuestionScope,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
            description="question scope",
        )

    async def expand_question(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> QueryExpansion:
        return await self._condensed(
            QueryExpansion,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
            description="query expansion",
        )

    async def follow_up_queries(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> FollowUpQueries:
        return await self._condensed(
            FollowUpQueries,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
            description="follow-up query list",
        )

    async def select_passages(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> PassageSelection:
        candidates = await self._structured_completion(
            client=self.rerank_client,
            config=self.config.rerank,
            schema=PassageSelection,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
        )
        return first_valid(PassageSelection, candidates, "passage selection")

    async def select_broad_passages(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> BroadSelection:
        candidates = await self._structured_completion(
            client=self.rerank_client,
            config=self.config.rerank,
            schema=BroadSelection,
            instructions=instructions,
            prompt=prompt,
            safety_identifier=safety_identifier,
        )
        return first_valid(BroadSelection, candidates, "broad passage selection")

    async def stream_answer(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        try:
            # Only opening the stream is retried; nothing has reached the visitor yet.
            stream = await with_connect_retry(
                lambda: self.chat_client.responses.create(
                    model=self.config.chat.deployment,
                    instructions=instructions,
                    input=prompt,
                    reasoning={"effort": self.config.chat.reasoning_effort},
                    text={"verbosity": self.config.chat.text_verbosity},
                    max_output_tokens=max_output_tokens or self.config.chat.max_output_tokens,
                    safety_identifier=safety_identifier,
                    store=False,
                    stream=True,
                )
            )
            completed = False
            received_text = False
            async with stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if not isinstance(delta, str):
                            raise ProviderError("Azure answer stream sent a malformed delta")
                        if delta:
                            received_text = True
                            yield delta
                    elif event_type in {"response.failed", "response.incomplete"}:
                        raise ProviderError(f"Azure response stream ended as {event_type}")
                    elif event_type == "response.completed":
                        completed = True
            if not completed or not received_text:
                raise ProviderError("Azure response stream was empty or had no completion event")
        except APIError as exc:
            raise ProviderError("Azure answer request failed") from exc
        except httpx.HTTPError as exc:  # transport failures while reading the stream
            raise ProviderError("Azure answer stream failed") from exc

    async def close(self) -> None:
        await self.chat_client.close()
        await self.condense_client.close()
        await self.embedding_client.close()
        await self.rerank_client.close()

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from flamingo_bot.chunking import SemanticChunker
from flamingo_bot.citizens import (
    BASE_URL,
    POSTS_URL,
    ROBOTS_URL,
    ArticleIndex,
    Checkpoint,
    Post,
    PublicReader,
    Response,
    _writer_lock,
    extract_article,
    movement_topics,
    normalize_url,
    plain_text,
    read_article_index,
    run_snapshot,
)
from flamingo_bot.config import Settings
from flamingo_bot.lapsi import RobotsPolicy, _SafeRedirect
from flamingo_bot.models import RetrievedChunk
from flamingo_bot.parsers import parse_sources
from flamingo_bot.planning import SOURCE_IDS, deduplicate
from flamingo_bot.prompting import build_grounded_prompt
from flamingo_bot.providers.memory import HashEmbedder, InMemoryVectorStore, ScriptedAnswerGenerator
from flamingo_bot.retrieval import ChatService
from flamingo_bot.sources import SourceCatalog, load_source_catalog, resolve_sources
from scripts.snapshot_citizens import main

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
POLICY = "User-agent: *\nDisallow: /wp-admin/\nDisallow: */feed/\nCrawl-delay: 10\n"
BODY = (
    "<p>Studentët protestuan në Tiranë kundër dhunës policore.</p>"
    "<p>Dëshmitarët kërkuan hetim të pavarur. Policia dha versionin e saj.</p>"
)


def post(identity: int = 1, **updates: Any) -> dict[str, Any]:
    return {
        "id": identity,
        "status": "publish",
        "date_gmt": "2026-10-20T12:00:00",
        "modified_gmt": "2026-10-20T13:00:00",
        "link": BASE_URL + f"/2026/10/20/studentet-ne-proteste-{identity}/",
        "title": {"rendered": "Studentët në protestë në Tiranë"},
        "content": {"rendered": BODY, "protected": False},
        "excerpt": {"rendered": "Studentët në Tiranë kërkojnë hetim të dhunës policore."},
        "_embedded": {
            "author": [{"name": "A. Reporteri"}],
            "wp:term": [
                [{"name": "Arsimi", "taxonomy": "category"}],
                [{"name": "protesta", "taxonomy": "post_tag"}],
            ],
        },
        **updates,
    }


class Site:
    def __init__(self, posts: list[dict[str, Any]] | None = None) -> None:
        self.posts = posts if posts is not None else [post()]
        self.calls: list[str] = []
        self.policy = POLICY
        self.fail_page: int | None = None
        self.incomplete = False

    def __call__(self, url: str) -> Response:
        self.calls.append(url)
        if url == ROBOTS_URL:
            return Response(text=self.policy)
        assert normalize_url(url).startswith(POSTS_URL)
        query = parse_qs(urlparse(url).query)
        page = int(query["page"][0])
        if self.fail_page == page:
            raise OSError("Synthetic network failure")
        values = self.posts
        if "modified_after" in query:
            values = [item for item in values if item["modified_gmt"] > query["modified_after"][0]]
        selected = values[(page - 1) * 50 : page * 50]
        return Response(
            text=json.dumps(selected[:-1] if self.incomplete else selected),
            total=len(values),
            pages=(len(values) + 49) // 50,
        )


def state(root: Path) -> Checkpoint:
    return Checkpoint.model_validate_json((root / "checkpoint.json").read_bytes())


def documents(root: Path):
    settings = Settings(_env_file=None, flamingo_citizens_dir=root)
    definition = next(
        item
        for item in load_source_catalog(settings.source_catalog_path).sources
        if item.id == "citizens"
    )
    sources = resolve_sources(settings, SourceCatalog(version=1, sources=[definition]))
    return parse_sources(sources)


def test_full_text_author_dates_and_canonical_citation_are_preserved(tmp_path: Path) -> None:
    site = Site([post(content={"rendered": BODY + "<p>DEEP BODY FACT</p>"})])
    report = run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    article = read_article_index(tmp_path / "index.json").articles[0]
    assert article.body.endswith("DEEP BODY FACT")
    assert article.author == "A. Reporteri"
    assert article.categories == ["Arsimi"] and article.tags == ["protesta"]
    assert article.published_at == "2026-10-20T12:00:00+00:00"
    assert article.content_use == "licensed_full_text"
    assert report["minimum_request_delay"] == 10
    assert report["model_calls"] == 0 and report["cloud_writes"] is False
    assert state(tmp_path).last_successful_scan == NOW.isoformat()


def test_dry_run_does_not_create_output(tmp_path: Path) -> None:
    output = tmp_path / "missing"
    report = run_snapshot(output, dry_run=True, fetch=Site(), now=NOW)
    assert report["new"] == 1 and report["checkpoint_advanced"] is False
    assert not output.exists()


@pytest.mark.parametrize("value", ["2026-05-30T21:59:59", "2026-10-22T00:00:00"])
def test_publication_bounds_use_gmt_not_url_date(value: str) -> None:
    assert extract_article(Post.model_validate(post(date_gmt=value)), now=NOW) is None


def test_may_31_tirana_midnight_is_included() -> None:
    article = extract_article(Post.model_validate(post(date_gmt="2026-05-30T22:00:00")), now=NOW)
    assert article is not None


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "draft"},
        {"status": "private"},
        {"content": {"rendered": BODY, "protected": True}},
        {
            "title": {"rendered": "Studentët në Paris protestojnë"},
            "excerpt": {"rendered": "Protesta në Francë"},
            "content": {"rendered": "<p>Studentët francezë protestuan në Paris.</p>" * 4},
        },
        {
            "title": {"rendered": "Programi i ri i bursave"},
            "excerpt": {"rendered": "Studentët marrin bursa në Tiranë"},
            "content": {"rendered": "<p>Hapet programi i ri i bursave në Tiranë.</p>" * 4},
        },
    ],
)
def test_unrelated_or_nonpublic_posts_are_excluded(updates: dict[str, Any]) -> None:
    assert extract_article(Post.model_validate(post(**updates)), now=NOW) is None


def test_article_body_can_establish_relevance_without_title_keywords() -> None:
    article = extract_article(
        Post.model_validate(
            post(
                title={"rendered": "Një zë që nuk hesht"},
                excerpt={"rendered": "Një rrëfim personal"},
                content={"rendered": BODY + "<p>Një rrëfim nga Revolucioni Flamingo.</p>"},
            )
        ),
        now=NOW,
    )
    assert article is not None


@pytest.mark.parametrize(
    "title,body",
    [
        ("Vukli në protestë për lumin Cem", "Banorët në Shqipëri kundërshtojnë HEC-et."),
        (
            "Divjaka në protestë kundër shkrirjes së bashkisë",
            "Banorët kundërshtojnë shkrirjen e bashkisë. Protestat qytetare vijojnë në Shqipëri.",
        ),
        (
            "Balluku përballet me akuza të reja",
            "SPAK në Tiranë heton çështjen. U njoftua një protestë.",
        ),
    ],
)
def test_unrelated_local_protests_and_incidental_news_mentions_are_excluded(
    title: str,
    body: str,
) -> None:
    assert movement_topics(title, "", body) == (0, [])


@pytest.mark.parametrize(
    "title,body",
    [
        ("Akademikët mbështesin protestat", "Mbështesin protestat qytetare në Shqipëri."),
        ("Gjyqi ndaj protestuesve", "Të arrestuarit pas protestës para Kuvendit të Shqipërisë."),
        ("Shoqërimet nga policia gjatë protestave", "Të drejtat për tubim në Shqipëri."),
        ("Vukli solidarizohet", "Banorët shprehën mbështetje për Revolucionin Flamingo."),
    ],
)
def test_movement_followups_rights_and_explicit_solidarity_remain_in_scope(
    title: str,
    body: str,
) -> None:
    assert movement_topics(title, "", body)[0] > 0


def test_protest_police_investigation_is_in_scope() -> None:
    title = "Plumbi në syrin e Vishkullit, vjen procedimi penal ndaj policisë gjatë protestave"
    excerpt = (
        "Prokuroria e Tiranës regjistroi procedimin ndaj Policisë së Shtetit "
        "që nga nisja e protestave 149 ditë më parë."
    )
    body = excerpt + " Ai u plagos në Tiranë; hetimi për përdorimin e forcës vazhdon."
    assert movement_topics(title, excerpt, body)[0] > 0


@pytest.mark.parametrize(
    "title,excerpt,body",
    [
        (
            "Nga ligji te gardhi me gjemba: si u dorëzuan zonat e mbrojtura",
            "Çfarë po ndodh në Zvërnec dhe në zonat e mbrojtura të Shqipërisë.",
            "Artikulli analizon ligjin dhe ndërtimet në zonën e mbrojtur të Zvërnecit.",
        ),
        (
            "Shfuqizimi i ligjit e lë derën hapur për investitorët strategjikë",
            "Projektligji zëvendësues mban në fuqi trajtimin për investitorët strategjikë.",
            "Artikulli shqyrton dispozitat e ligjit dhe projektin e resortit në Sazan.",
        ),
    ],
)
def test_reports_on_protest_causes_remain_in_scope(title: str, excerpt: str, body: str) -> None:
    score, topics = movement_topics(title, excerpt, body)
    assert score > 0
    assert "protest-causes" in topics


@pytest.mark.parametrize(
    "title,excerpt,body",
    [
        ("Turistët vizitojnë Sazanin", "Pushime në det", "Udhëtim në ishullin e Sazanit."),
        (
            "Policia heton përleshjet në Francë",
            "Hetim për protestën në Paris.",
            "Policia franceze heton protestat në Paris.",
        ),
        (
            "Polici dhe Gardë pranë resorteve në Palasë",
            "Projekt për një qendër të re shërbimesh publike.",
            "Ndërtimi në Palasë është pjesë e një tjetër projekti.\n\n"
            "Lexo edhe: ankandet e 2025 nga Sazani te një zonë tjetër.",
        ),
    ],
)
def test_unrelated_travel_and_foreign_investigations_remain_excluded(
    title: str, excerpt: str, body: str
) -> None:
    assert movement_topics(title, excerpt, body) == (0, [])


def test_fragment_extraction_removes_executable_and_navigation_text() -> None:
    assert (
        plain_text(
            "<p>One <b>bold</b> &amp; clear.</p><script>BAD</script>"
            "<nav><span>MENU</span></nav><iframe>VIDEO</iframe><p>Two.</p>"
        )
        == "One bold & clear.\n\nTwo."
    )


def test_incremental_edits_keep_history_and_unchanged_revisions(tmp_path: Path) -> None:
    site = Site([post(), post(2)])
    run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    before = (tmp_path / "index.json").read_bytes()
    report = run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW + timedelta(hours=1))
    assert report["new"] == 0 and report["updated"] == 0 and report["full_rescan"] is False
    assert (tmp_path / "index.json").read_bytes() == before
    assert "modified_after=" in site.calls[-1]
    site.posts[0]["modified_gmt"] = "2026-10-21T13:00:00"
    site.posts[0]["content"]["rendered"] += "<p>New testimony.</p>"
    report = run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW + timedelta(hours=2))
    assert report["updated"] == 1 and report["selected"] == 2


def test_full_rescan_removes_withdrawn_posts_but_incremental_does_not(tmp_path: Path) -> None:
    site = Site([post(), post(2)])
    run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    site.posts.pop()
    assert (
        run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW + timedelta(days=1))["removed"]
        == 0
    )
    report = run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW + timedelta(days=30))
    assert report["full_rescan"] is True and report["removed"] == 1


def test_edited_post_that_loses_relevance_is_removed(tmp_path: Path) -> None:
    site = Site()
    run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    site.posts = [
        post(
            modified_gmt="2026-10-21T12:30:00",
            title={"rendered": "Lajme të tjera"},
            excerpt={"rendered": "Një lajm i palidhur"},
            content={"rendered": "Unrelated news. " * 20},
        )
    ]
    report = run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW + timedelta(hours=1))
    assert report["removed"] == 1


@pytest.mark.parametrize("failure", ["network", "incomplete", "limit", "duplicate"])
def test_partial_scan_never_changes_index_or_checkpoint(tmp_path: Path, failure: str) -> None:
    site = Site()
    run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    old_index = (tmp_path / "index.json").read_bytes()
    old_checkpoint = (tmp_path / "checkpoint.json").read_bytes()
    site.posts = [post(index) for index in range(1, 52)]
    if failure == "network":
        site.fail_page = 2
    elif failure == "incomplete":
        site.incomplete = True
    elif failure == "duplicate":
        site.posts[-1] = post()
    with pytest.raises((OSError, ValueError)):
        run_snapshot(
            tmp_path,
            dry_run=False,
            fetch=site,
            now=NOW + timedelta(hours=1),
            full_rescan=True,
            max_articles=50 if failure == "limit" else 5000,
        )
    assert (tmp_path / "index.json").read_bytes() == old_index
    assert (tmp_path / "checkpoint.json").read_bytes() == old_checkpoint


def test_successful_pagination_collects_every_post(tmp_path: Path) -> None:
    report = run_snapshot(
        tmp_path, dry_run=True, fetch=Site([post(i) for i in range(1, 54)]), now=NOW
    )
    assert report["articles_checked"] == 53 and report["selected"] == 53


def test_empty_archive_is_a_valid_complete_scan(tmp_path: Path) -> None:
    assert run_snapshot(tmp_path, dry_run=True, fetch=Site([]), now=NOW)["selected"] == 0


@pytest.mark.parametrize("change", ["missing_index", "filter_version", "digest_mismatch"])
def test_inconsistent_checkpoint_forces_full_reconciliation(tmp_path: Path, change: str) -> None:
    site = Site()
    run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    if change == "missing_index":
        (tmp_path / "index.json").unlink()
    else:
        checkpoint = state(tmp_path)
        checkpoint = checkpoint.model_copy(
            update={
                "filter_version" if change == "filter_version" else "index_revision": "outdated",
            }
        )
        (tmp_path / "checkpoint.json").write_text(checkpoint.model_dump_json())
    report = run_snapshot(tmp_path, dry_run=True, fetch=site, now=NOW + timedelta(days=1))
    assert report["full_rescan"] is True and report["selected"] == 1
    assert "modified_after=" not in site.calls[-1]


def test_malformed_relevant_article_does_not_advance_checkpoint(tmp_path: Path) -> None:
    site = Site([post(content={"rendered": "<p>Too short</p>"})])
    with pytest.raises(ValueError):
        run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    assert not (tmp_path / "index.json").exists()
    assert not (tmp_path / "checkpoint.json").exists()


@pytest.mark.parametrize(
    "rule",
    [
        "Disallow: /wp-json/",
        "Content-Signal: use=reference",
        "Content-Signal: ai-input=no",
    ],
)
def test_crawl_policy_changes_block_collection(tmp_path: Path, rule: str) -> None:
    site = Site()
    site.policy = POLICY + rule + "\n"
    with pytest.raises(ValueError):
        run_snapshot(tmp_path, dry_run=False, fetch=site, now=NOW)
    assert site.calls == [ROBOTS_URL]
    assert not (tmp_path / "checkpoint.json").exists()


def test_reader_throttles_initial_requests_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = PublicReader()
    clock = [100.0]
    waits: list[float] = []

    def sleep(value: float) -> None:
        waits.append(value)
        clock[0] += value

    monkeypatch.setattr("flamingo_bot.citizens.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("flamingo_bot.citizens.time.sleep", sleep)
    reader._before_request(ROBOTS_URL)
    reader.policy = RobotsPolicy(POLICY.replace("10", "15"))
    reader._before_request(POSTS_URL)
    handler = _SafeRedirect(reader._before_request)
    handler.redirect_request(Request(POSTS_URL), None, 302, "Redirect", {}, POSTS_URL + "?page=1")
    assert waits == [15, 15]
    with pytest.raises(ValueError):
        handler.redirect_request(
            Request(POSTS_URL), None, 302, "Redirect", {}, "https://evil.test/"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://citizens.al/robots.txt",
        "https://user@citizens.al/robots.txt",
        "https://citizens.al:443/robots.txt",
        "https://citizens.al.evil.test/robots.txt",
        "https://citizens.al/wp-admin/",
        "https://citizens.al/feed/",
        "https://citizens.al/?s=protest",
        "https://citizens.al/wp-json/wp/v2/posts?context=edit",
        "https://citizens.al/wp-json/wp/v2/%2e%2e/posts",
    ],
)
def test_reader_never_fetches_private_unapproved_or_external_resources(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_single_writer_guard(tmp_path: Path) -> None:
    with _writer_lock(tmp_path, dry_run=False), pytest.raises(ValueError, match="already running"):
        with _writer_lock(tmp_path, dry_run=False):
            pass


def test_full_text_chunking_provenance_and_format_version(tmp_path: Path) -> None:
    body = BODY + "<p>Detailed testimony by a witness. </p>" * 100 + "<p>FINAL EVIDENCE</p>"
    run_snapshot(tmp_path, dry_run=False, fetch=Site([post(content={"rendered": body})]), now=NOW)
    docs, issues, _ = documents(tmp_path)
    assert not issues and len(docs) == 1
    assert docs[0].metadata["publisher"] == "Citizens.al"
    assert docs[0].metadata["parser_format"] == "citizens-article/1"
    assert "event_date" not in docs[0].metadata
    chunks = SemanticChunker().chunk_documents(docs)
    assert len(chunks) > 1
    assert "FINAL EVIDENCE" in chunks[-1].text
    assert all(item.canonical_url == post()["link"] for item in chunks)
    assert all("Byline: A. Reporteri" in item.text for item in chunks)
    checkpoint = (tmp_path / "checkpoint.json").read_text()
    (tmp_path / "checkpoint.json").write_text(checkpoint + "\n")
    assert documents(tmp_path)[0][0].revision == docs[0].revision


def test_citizens_retrieval_and_prompt_can_summarize_body_and_cite_original(tmp_path: Path) -> None:
    run_snapshot(tmp_path, dry_run=False, fetch=Site(), now=NOW)
    docs, _, _ = documents(tmp_path)
    chunk = SemanticChunker().chunk_documents(docs)[0]
    retrieved = RetrievedChunk(**chunk.model_dump(), distance=0.1)
    assert "citizens" in SOURCE_IDS
    prompt = build_grounded_prompt("What did the witnesses say?", [retrieved])
    assert "Licensed full-text passage" in prompt and "Reference-only use:" not in prompt
    assert "Dëshmitarët kërkuan" in prompt
    assert deduplicate([retrieved, retrieved]) == [retrieved]

    async def scenario() -> None:
        embedder = HashEmbedder()
        question = "What did the witnesses say?"
        vector = (await embedder.embed_texts([question]))[0]
        service = ChatService(
            settings=Settings(_env_file=None),
            embedder=embedder,
            vector_store=InMemoryVectorStore([(retrieved, vector)]),
            answer_generator=ScriptedAnswerGenerator("Testimony [S1]."),
        )
        citations, _ = await service.stream(question)
        assert citations[0].source == "Citizens.al" and citations[0].url == post()["link"]

    asyncio.run(scenario())


def test_invalid_index_and_bad_cli_options_fail_closed(tmp_path: Path) -> None:
    article = extract_article(Post.model_validate(post()), now=NOW)
    assert article is not None
    with pytest.raises(ValueError, match="Duplicate"):
        ArticleIndex(articles=[article, article])
    assert main(["--dry-run", "--delay", "0", "--output", str(tmp_path)]) == 1

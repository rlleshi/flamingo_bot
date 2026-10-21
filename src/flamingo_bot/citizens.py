"""Licensed, protest-only Citizens.al full-text snapshots via its public API.

The owner confirmed permission for full-text indexing. This collector still obeys
current crawl restrictions, never authenticates, and never publishes to Firestore.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flamingo_bot.lapsi import (
    PROTEST_START,
    RobotsPolicy,
    _atomic_json,
    _SafeRedirect,
    protest_topics,
)

BASE_URL = "https://citizens.al"
ROBOTS_URL = BASE_URL + "/robots.txt"
POSTS_URL = BASE_URL + "/wp-json/wp/v2/posts"
USER_AGENT = "FlamingoBotProtestArticles/1.0 (+https://www.flamingorevolution.eu/)"
FORMAT_VERSION: Literal["citizens-article/1"] = "citizens-article/1"
FILTER_VERSION = "citizens-protest-topics/2"
MAX_RESPONSE_BYTES = 8_000_000
_QUERY_KEYS = {
    "per_page",
    "page",
    "after",
    "modified_after",
    "modified_before",
    "orderby",
    "order",
    "status",
    "_embed",
    "_fields",
}


def normalize_url(value: str, *, article: bool = False) -> str:
    parsed = urlparse(value)
    decoded = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"citizens.al", "www.citizens.al"}
        or parsed.params
        or "\\" in value
        or "\\" in decoded
        or any(ord(char) < 32 for char in value + decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("Only public HTTPS Citizens.al URLs are allowed")
    if article:
        if not re.fullmatch(r"/\d{4}/\d{2}/\d{2}/[^/\s]+/?", decoded):
            raise ValueError("Expected a dated Citizens.al article URL")
        return BASE_URL + parsed.path.rstrip("/") + "/"
    if parsed.path not in {"/robots.txt", "/wp-json/wp/v2/posts"} or parsed.fragment:
        raise ValueError("Only robots.txt and the public Citizens posts API may be fetched")
    if set(parse_qs(parsed.query)) - _QUERY_KEYS:
        raise ValueError("Unexpected Citizens API query parameter")
    return BASE_URL + parsed.path + ("?" + parsed.query if parsed.query else "")


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Snapshot timestamps must include a timezone")
    return parsed.astimezone(UTC)


class _ArticleText(HTMLParser):
    """Extract a WordPress content fragment, not theme/navigation or executable HTML."""

    _SKIP = frozenset({"script", "style", "noscript", "nav", "form", "iframe", "svg", "button"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "tr", "figure"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skipped.append(tag)
        if not self.skipped and tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _ArticleText()
    parser.feed(value)
    parser.close()
    return "\n\n".join(
        line for part in "".join(parser.parts).split("\n\n") if (line := " ".join(part.split()))
    )


def movement_topics(title: str, excerpt: str, body: str) -> tuple[int, list[str]]:
    """Require a movement/cause link, not just an Albanian news/protest keyword."""
    score, topics = protest_topics(title, excerpt)
    if not score:
        score, topics = protest_topics(title, body)
    if not score:
        return 0, []

    def fold(value: str) -> str:
        return " ".join(
            "".join(
                char
                for char in unicodedata.normalize("NFKD", value.casefold())
                if not unicodedata.combining(char)
            ).split()
        )

    text, lead = fold(title + " " + body), fold(title + " " + excerpt + " " + body[:800])
    movement = bool(
        re.search(
            r"flaming\w*|diaspora zbarkon|zvernec|\bnart\w*|sazan|rrjoll|kakome|mertur|"
            r"shqiperia nuk (?:eshte ne shitje|shitet)|21/2024|ligj\w*.{0,40}zon\w*.{0,20}mbrojt",
            text,
        )
    )
    separate_local_dispute = bool(
        re.search(
            r"banor\w*.{0,100}(?:shkrir|riorganiz|hec\b|hidrocentr|demshperbl)",
            lead,
        )
    )
    if separate_local_dispute and not movement:
        return 0, []
    # Event/legal follow-ups sometimes omit the movement's name. Require the
    # protest in the lead AND a specific civic/student/central-protest context.
    central_protest = bool(
        re.search(
            r"protest\w*.{0,100}(?:para|prane|perballe|tek|te) "
            r"(?:kuvend|parlament|kryeministr|komisariat)|"
            r"protest\w*.{0,30}(?:perditshme|qytetare)|student\w*.{0,60}protest|"
            r"protest\w*.{0,60}student|te drejt\w*.{0,40}(?:tubim|protest)",
            text,
        )
    )
    protest_rights = bool(
        re.search(r"avokat|gazetar|dhun\w*.{0,20}polic|polic\w*.{0,20}dhun", fold(title))
        and re.search(r"protest\w*", lead)
        and re.search(r"helsink|res publica|sejran|liri\w*.{0,30}(?:medi|tubim)", text)
    )
    if (
        movement
        or protest_rights
        or (re.search(r"protest|revolt|marshim", lead) and central_protest)
    ):
        return score, topics
    return 0, []


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: int = Field(gt=0)
    url: str
    title: str = Field(min_length=5, max_length=500)
    body: str = Field(min_length=80, max_length=200_000)
    author: str = Field(min_length=1, max_length=500)
    categories: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=100)
    published_at: str
    modified_at: str
    captured_at: str
    topics: list[str] = Field(min_length=1, max_length=6)
    relevance_score: int = Field(ge=1, le=100)
    content_use: Literal["licensed_full_text"] = "licensed_full_text"
    permission_basis: Literal["project-owner-confirmed"] = "project-owner-confirmed"

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return normalize_url(value, article=True)

    @field_validator("published_at", "modified_at", "captured_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return timestamp(value).isoformat()

    @property
    def revision(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(exclude={"captured_at"}), sort_keys=True).encode()
        ).hexdigest()


class ArticleIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["citizens-article/1"] = FORMAT_VERSION
    articles: list[Article] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def unique(self) -> ArticleIndex:
        if len({item.post_id for item in self.articles}) != len(self.articles):
            raise ValueError("Duplicate Citizens post IDs")
        if len({item.url for item in self.articles}) != len(self.articles):
            raise ValueError("Duplicate Citizens canonical URLs")
        return self


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["citizens-checkpoint/1"] = "citizens-checkpoint/1"
    filter_version: str = FILTER_VERSION
    last_successful_scan: str
    last_full_scan: str
    index_revision: str

    @field_validator("last_successful_scan", "last_full_scan")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return timestamp(value).isoformat()


def read_article_index(path: Path) -> ArticleIndex:
    raw = path.read_bytes()
    if len(raw) > 80_000_000:
        raise ValueError("The Citizens snapshot exceeds 80 MB")
    return ArticleIndex.model_validate_json(raw)


class Rendered(BaseModel):
    rendered: str
    protected: bool = False


class Post(BaseModel):
    """Only fields from the unauthenticated WordPress public response are used."""

    id: int = Field(gt=0)
    status: str
    date_gmt: str
    modified_gmt: str
    link: str
    title: Rendered
    content: Rendered
    excerpt: Rendered
    embedded: dict[str, Any] = Field(default_factory=dict, alias="_embedded")


def _gmt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def extract_article(post: Post, *, now: datetime) -> Article | None:
    if post.status != "publish" or post.content.protected:
        return None
    publication, modified = _gmt(post.date_gmt), _gmt(post.modified_gmt)
    if not PROTEST_START <= publication <= now:
        return None
    title, body = plain_text(post.title.rendered), plain_text(post.content.rendered)
    score, topics = movement_topics(title, plain_text(post.excerpt.rendered), body)
    if not score:
        return None
    authors = post.embedded.get("author", [])
    byline = (
        ", ".join(
            plain_text(item["name"])
            for item in authors
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        or "Citizens.al"
    )
    terms: dict[str, list[str]] = {"category": [], "post_tag": []}
    for group in post.embedded.get("wp:term", []):
        for item in group:
            if isinstance(item, dict) and item.get("taxonomy") in terms:
                name = item.get("name")
                if isinstance(name, str):
                    terms[item["taxonomy"]].append(plain_text(name))
    return Article(
        post_id=post.id,
        url=post.link,
        title=title,
        body=body,
        author=byline,
        categories=sorted(set(terms["category"])),
        tags=sorted(set(terms["post_tag"])),
        published_at=publication.isoformat(),
        modified_at=modified.isoformat(),
        captured_at=now.isoformat(),
        topics=topics,
        relevance_score=score,
    )


class Response(BaseModel):
    text: str
    total: int | None = Field(default=None, ge=0)
    pages: int | None = Field(default=None, ge=0)


class PublicReader:
    def __init__(self, *, delay: float = 10) -> None:
        if not 10 <= delay <= 60:
            raise ValueError("Citizens request delay must be between 10 and 60 seconds")
        self.delay = delay
        self.policy: RobotsPolicy | None = None
        self.last_request: float | None = None
        self.opener = build_opener(_SafeRedirect(self._before_request))

    def _before_request(self, url: str) -> None:
        normalize_url(url)
        if self.policy:
            self.policy.check(url)
        delay = max(self.delay, self.policy.delay if self.policy else 0)
        if self.last_request is not None:
            wait = self.last_request + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self.last_request = time.monotonic()

    def __call__(self, url: str) -> Response:
        self._before_request(url)
        with self.opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as raw:
            normalize_url(raw.geturl())
            kind = raw.headers.get_content_type()
            expected = {"text/plain"} if url == ROBOTS_URL else {"application/json"}
            if kind not in expected:
                raise ValueError("Unexpected Citizens response content type")
            data: bytes = raw.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Citizens response exceeds 8 MB")
            return Response(
                text=data.decode("utf-8"),
                total=raw.headers.get("X-WP-Total"),
                pages=raw.headers.get("X-WP-TotalPages"),
            )


@contextmanager
def _writer_lock(root: Path, *, dry_run: bool) -> Iterator[None]:
    if dry_run:
        yield
        return
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / ".snapshot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Citizens collector is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_snapshot(
    output: Path,
    *,
    dry_run: bool,
    full_rescan: bool = False,
    max_articles: int = 5000,
    overlap_hours: int = 48,
    delay: float = 10,
    fetch: Callable[[str], Response] | None = None,
    now: datetime | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not 1 <= max_articles <= 20_000 or not 1 <= overlap_hours <= 168:
        raise ValueError("Invalid article limit (1-20000) or overlap hours (1-168)")
    reader = PublicReader(delay=delay)
    now = timestamp((now or datetime.now(UTC)).isoformat())
    if now < PROTEST_START:
        raise ValueError("Scan end precedes 31 May 2026")
    with _writer_lock(output, dry_run=dry_run):
        return _scan(
            output,
            dry_run=dry_run,
            full_rescan=full_rescan,
            max_articles=max_articles,
            overlap_hours=overlap_hours,
            reader=reader,
            fetch=fetch or reader,
            now=now,
            progress=progress,
        )


def _scan(
    output: Path,
    *,
    dry_run: bool,
    full_rescan: bool,
    max_articles: int,
    overlap_hours: int,
    reader: PublicReader,
    fetch: Callable[[str], Response],
    now: datetime,
    progress: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    path = output / "index.json"
    previous = read_article_index(path) if path.exists() else ArticleIndex()
    checkpoint_path = output / "checkpoint.json"
    checkpoint = (
        Checkpoint.model_validate_json(checkpoint_path.read_bytes())
        if checkpoint_path.exists()
        else None
    )
    digest = hashlib.sha256(previous.model_dump_json().encode()).hexdigest()
    full = (
        full_rescan
        or checkpoint is None
        or checkpoint.filter_version != FILTER_VERSION
        or checkpoint.index_revision != digest
        or now - timestamp(checkpoint.last_full_scan) >= timedelta(days=30)
    )
    if checkpoint and timestamp(checkpoint.last_successful_scan) > now:
        raise ValueError("Checkpoint is ahead of the current clock")
    since = (
        PROTEST_START.astimezone(UTC)
        if full or checkpoint is None
        else max(
            PROTEST_START,
            timestamp(checkpoint.last_successful_scan) - timedelta(hours=overlap_hours),
        ).astimezone(UTC)
    )
    policy = RobotsPolicy(fetch(ROBOTS_URL).text, robots_url=ROBOTS_URL, user_agent=USER_AGENT)
    if policy.signals.get("use") == "reference":
        raise ValueError(
            "Citizens now requests reference-only use; review before full-text indexing"
        )
    reader.policy = policy
    parameters: dict[str, str | int] = {
        "per_page": 50,
        "after": "2026-05-30T00:00:00",
        "status": "publish",
        "orderby": "id",
        "order": "asc",
        "_embed": "author,wp:term",
        "_fields": "id,status,date_gmt,modified_gmt,link,title,content,excerpt,_embedded,_links",
    }
    if not full:
        parameters["modified_after"] = since.strftime("%Y-%m-%dT%H:%M:%S")
    old = {article.post_id: article for article in previous.articles}
    current = {} if full else dict(old)
    seen: set[int] = set()
    total: int | None = None
    pages = 1
    page = 1
    while page <= pages:
        url = POSTS_URL + "?" + urlencode({**parameters, "page": page})
        policy.check(url)
        response = fetch(url)
        if response.total is None or response.pages is None:
            raise ValueError("Citizens API omitted pagination totals; checkpoint not advanced")
        if total is None:
            total, pages = response.total, response.pages
            if total > max_articles:
                raise ValueError(
                    "Citizens scan exceeds --max-articles; refusing a partial snapshot"
                )
            if pages != (total + 49) // 50:
                raise ValueError("Inconsistent Citizens pagination totals")
        elif (response.total, response.pages) != (total, pages):
            raise ValueError("Citizens archive changed during pagination; rerun the scan")
        raw = json.loads(response.text)
        if not isinstance(raw, list) or len(raw) != min(50, total - 50 * (page - 1)):
            raise ValueError("Incomplete Citizens API page; checkpoint not advanced")
        for item in raw:
            post = Post.model_validate(item)
            if post.id in seen:
                raise ValueError("Duplicate post across Citizens pages; rerun the scan")
            seen.add(post.id)
            article = extract_article(post, now=now)
            if article is None:
                current.pop(post.id, None)
            else:
                existing = old.get(post.id)
                current[post.id] = (
                    existing if existing and existing.revision == article.revision else article
                )
        if progress:
            progress({"page": page, "pages": pages, "checked": len(seen), "selected": len(current)})
        page += 1
    if len(seen) != total:
        raise ValueError("Incomplete Citizens archive scan")
    index = ArticleIndex(articles=sorted(current.values(), key=lambda article: article.post_id))
    new = sum(key not in old for key in current)
    updated = sum(
        key in old and item.revision != old[key].revision for key, item in current.items()
    )
    removed = len(old.keys() - current.keys())
    if not dry_run:
        _atomic_json(path, index)
        _atomic_json(
            checkpoint_path,
            Checkpoint(
                last_successful_scan=now.isoformat(),
                last_full_scan=now.isoformat()
                if full or checkpoint is None
                else checkpoint.last_full_scan,
                index_revision=hashlib.sha256(index.model_dump_json().encode()).hexdigest(),
            ),
        )
    return {
        "mode": "dry-run" if dry_run else "write",
        "full_rescan": full,
        "scan_from": since.isoformat(),
        "articles_checked": len(seen),
        "selected": len(current),
        "new": new,
        "updated": updated,
        "removed": removed,
        "checkpoint_advanced": not dry_run,
        "model_calls": 0,
        "cloud_writes": False,
        "content_use": "licensed_full_text",
        "minimum_request_delay": max(reader.delay, policy.delay),
        "preview": [
            {"title": item.title, "url": item.url, "characters": len(item.body)}
            for item in index.articles[:10]
        ],
    }

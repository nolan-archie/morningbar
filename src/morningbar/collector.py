from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from time import struct_time
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import httpx
from icalendar import Calendar
from rapidfuzz import fuzz
import trafilatura

from .models import Article, InterestsConfig, SourceConfig, Story, TopicConfig
from .status import StatusWriter
from .storage import Store
from .urls import normalize_url, stable_id

log = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5
ANILIST_QUERY = """
query ($page: Int, $perPage: Int, $ids: [Int]) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC, id_in: $ids) {
      id
      title { romaji english }
      siteUrl
      nextAiringEpisode { airingAt episode }
    }
  }
}
"""


class HostLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        # arXiv asks clients to wait at least three seconds between API queries.
        self.overrides = {"export.arxiv.org": 3.0}
        self.last: dict[str, float] = {}
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        async with self.locks[host]:
            loop = asyncio.get_running_loop()
            interval = max(self.interval, self.overrides.get(host, 0.0))
            delay = interval - (loop.time() - self.last.get(host, -1e9))
            if delay > 0:
                await asyncio.sleep(delay)
            self.last[host] = loop.time()


class RunBudgetExceeded(RuntimeError):
    pass


class RunByteBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.lock = asyncio.Lock()

    async def chunk_size(self) -> int:
        async with self.lock:
            if self.used >= self.limit:
                raise RunBudgetExceeded("run response-body budget is exhausted")
            return min(16_384, self.limit - self.used)

    async def consume(self, amount: int) -> None:
        async with self.lock:
            if self.used + amount > self.limit:
                raise RunBudgetExceeded("run response-body budget is exhausted")
            self.used += amount


@dataclass
class FetchResult:
    articles: list[Article]
    unchanged: bool = False


class Collector:
    def __init__(self, config: InterestsConfig, store: Store, status: StatusWriter):
        self.config = config
        self.store = store
        self.status = status
        self.limiter = HostLimiter(config.settings.request_interval_seconds)
        self.byte_budget = RunByteBudget(config.settings.max_body_bytes)
        self.topics = {topic.id: topic for topic in config.topics}
        self.robots_cache: dict[str, RobotFileParser] = {}
        self.client: httpx.AsyncClient | None = None

    async def collect(self, *, offline: bool = False) -> tuple[list[Story], int, int]:
        now = datetime.now(UTC)
        lookback = now - timedelta(hours=self.config.settings.lookback_hours)
        cooldown = now - timedelta(hours=self.config.settings.shown_cooldown_hours)
        self.store.prune(lookback - timedelta(days=1), cooldown)
        cached_shown = self.store.shown_ids(cooldown)
        sources = self.config.sources
        self.status.update("collecting", 0, len(sources), unavailable=0)
        unavailable = 0
        gathered: list[Article] = []
        if offline:
            self.status.update("collecting-cache", 0, len(sources), unavailable=0)
            for index, source in enumerate(sources, start=1):
                gathered.extend(self.store.cached_articles(source.id, lookback))
                self.status.update(
                    "collecting-cache", index, len(sources), unavailable=0,
                    active_source=source.id,
                )
        else:
            semaphore = asyncio.Semaphore(12)
            done = 0
            progress_lock = asyncio.Lock()

            async def one(source: SourceConfig) -> None:
                nonlocal done, unavailable
                try:
                    async with semaphore:
                        result = await self.fetch_source(source, lookback)
                    gathered.extend(result.articles)
                except Exception as exc:  # one source must never abort the briefing
                    unavailable += 1
                    log.warning("source %s unavailable: %s", source.id, type(exc).__name__)
                finally:
                    async with progress_lock:
                        done += 1
                        self.status.update(
                            "collecting", done, len(sources), unavailable=unavailable,
                            active_source=source.id,
                        )

            limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=False,
                limits=limits,
                headers={
                    "User-Agent": "MorningBar/0.1 (personal local news reader)",
                    "Accept": "application/atom+xml, application/rss+xml, application/xml, application/json, text/html;q=0.9, text/xml;q=0.8, */*;q=0.5",
                },
                http2=True,
            ) as client:
                self.client = client
                try:
                    await asyncio.gather(*(one(source) for source in sources))
                finally:
                    self.client = None
            if not sources:
                self.status.update("collecting", 0, 0, unavailable=0)

        fresh = [article for article in gathered if article.published_at >= lookback]
        stories = cluster_and_rank(fresh, self.topics)
        adjustments = self.store.topic_score_adjustments()
        if adjustments:
            stories = [
                story.model_copy(update={"score": story.score + adjustments.get(story.topic_id, 0.0)})
                for story in stories
            ]
            stories.sort(key=lambda story: (story.manual_priority, story.score), reverse=True)
        stories = filter_shown_stories(stories, cached_shown)
        stories = stories[: self.config.settings.max_candidates]
        self.status.update(
            "collected", len(sources), len(sources), unavailable=unavailable,
            articles=len(fresh), candidates=len(stories), body_bytes=self.byte_budget.used,
            offline=offline,
        )
        return stories, unavailable, 0 if offline else len(sources)

    async def fetch_source(self, source: SourceConfig, since: datetime) -> FetchResult:
        if source.kind == "hackernews":
            return await self.fetch_hackernews(source, since)
        if source.kind == "huggingface":
            return await self.fetch_huggingface(source, since)
        if source.kind == "anilist":
            return await self.fetch_anilist(source, since)
        if source.kind == "ical":
            return await self.fetch_ical(source, since)
        if source.kind == "page":
            return await self.fetch_page(source, since)
        return await self.fetch_feed(source, since)

    async def get_bytes(
        self,
        url: str,
        headers: dict[str, str],
        *,
        max_bytes: int = MAX_RESPONSE_BYTES,
        rate_retries: int = 1,
        json_data: dict[str, Any] | None = None,
    ) -> tuple[int, httpx.Headers, bytes, str]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            chunk_size = await self.byte_budget.chunk_size()
            await self.limiter.wait(current)
            if self.client is None:
                raise RuntimeError("HTTP client is not open")
            method = "POST" if json_data is not None else "GET"
            request_headers = dict(headers)
            if json_data is not None:
                request_headers.setdefault("Content-Type", "application/json")
            async with self.client.stream(
                method, current, headers=request_headers, json=json_data
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        response.raise_for_status()
                    current = urljoin(str(response.url), location)
                    continue
                if response.status_code == 429:
                    if rate_retries <= 0:
                        response.raise_for_status()
                    retry_after = response.headers.get("retry-after", "1")
                    try:
                        wait_for = min(30.0, max(1.0, float(retry_after)))
                    except ValueError:
                        try:
                            wait_for = min(30.0, max(1.0, (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()))
                        except (TypeError, ValueError):
                            wait_for = 2.0
                    await asyncio.sleep(wait_for)
                    return await self.get_bytes(
                        current, headers, max_bytes=max_bytes, rate_retries=rate_retries - 1,
                        json_data=json_data,
                    )
                if response.status_code == 304:
                    return response.status_code, response.headers, b"", current
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ValueError("response exceeds configured body limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("response exceeds configured body limit")
                    await self.byte_budget.consume(len(chunk))
                    chunks.append(chunk)
                return response.status_code, response.headers, b"".join(chunks), current
        raise ValueError("too many redirects")

    async def fetch_feed(self, source: SourceConfig, since: datetime) -> FetchResult:
        headers = self.store.request_headers(source.id)
        status, response_headers, body, final_url = await self.get_bytes(source.url, headers)
        if status == 304:
            return FetchResult(self.store.cached_articles(source.id, since), unchanged=True)
        self.store.set_response_cache(
            source.id,
            response_headers.get("etag"),
            response_headers.get("last-modified"),
        )
        items = parse_feed_bytes(
            body,
            source,
            max_items=min(source.max_items, self.config.settings.max_feed_items),
            fetched_at=datetime.now(UTC),
            final_url=final_url,
        )
        self.store.save_articles(items)
        return FetchResult(items)

    async def fetch_hackernews(self, source: SourceConfig, since: datetime) -> FetchResult:
        top_url = source.url.rstrip("/") + "/topstories.json"
        status, _, body, base = await self.get_bytes(top_url, {})
        if status != 200:
            raise ValueError(f"unexpected Hacker News API status {status}")
        ids = json.loads(body)
        if not isinstance(ids, list):
            raise ValueError("Hacker News topstories response was not a list")
        limit = min(source.max_items, self.config.settings.max_feed_items, 40)
        result: list[Article] = []
        for item_id in ids[:limit]:
            item_url = urljoin(base, f"item/{int(item_id)}.json")
            try:
                item_status, _, item_body, item_base = await self.get_bytes(item_url, {})
                if item_status != 200:
                    continue
                data = json.loads(item_body)
                if not isinstance(data, dict) or data.get("type") != "story":
                    continue
                timestamp = datetime.fromtimestamp(int(data.get("time", 0)), UTC)
                if timestamp < since or data.get("deleted") or data.get("dead"):
                    continue
                title = clean_text(data.get("title", ""), 300)
                link = data.get("url") or f"https://news.ycombinator.com/item?id={item_id}"
                excerpt = clean_text(data.get("text", ""), 600)
                if data.get("score") is not None:
                    score_text = f"HN score {data['score']}"
                    excerpt = f"{score_text}. {excerpt}" if excerpt else score_text
                result.append(
                    Article(
                        id=stable_id(f"{source.id}\n{normalize_url(link)}"),
                        topic_id=source.topic,
                        source_id=source.id,
                        source_name=source.name,
                        source_host=(urlsplit(item_base).hostname or "").lower(),
                        trust=source.trust,
                        title=title,
                        link=normalize_url(link),
                        published_at=timestamp,
                        excerpt=excerpt,
                    )
                )
            except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
                log.debug("Hacker News item %s skipped: %s", item_id, type(exc).__name__)
        self.store.save_articles(result)
        return FetchResult(result)

    async def fetch_huggingface(self, source: SourceConfig, since: datetime) -> FetchResult:
        headers = self.store.request_headers(source.id)
        status, response_headers, body, final_url = await self.get_bytes(source.url, headers)
        if status == 304:
            return FetchResult(self.store.cached_articles(source.id, since), unchanged=True)
        self.store.set_response_cache(
            source.id,
            response_headers.get("etag"),
            response_headers.get("last-modified"),
        )
        result = parse_huggingface_bytes(
            body,
            source,
            since=since,
            max_items=min(source.max_items, self.config.settings.max_feed_items),
            fetched_at=datetime.now(UTC),
            final_url=final_url,
            keywords=self.topics[source.topic].keywords,
        )
        self.store.save_articles(result)
        return FetchResult(result)

    async def fetch_anilist(self, source: SourceConfig, since: datetime) -> FetchResult:
        topic = self.topics[source.topic]
        watchlist = topic.watchlist
        ids: list[int] | None = None
        if watchlist:
            try:
                ids = [int(item) for item in watchlist]
            except ValueError as exc:
                raise ValueError("AniList watchlist entries must be numeric media ids") from exc
        payload = {
            "query": ANILIST_QUERY,
            "variables": {"page": 1, "perPage": 50, "ids": ids},
        }
        status, _, body, _ = await self.get_bytes(source.url, {}, json_data=payload)
        if status != 200:
            raise ValueError(f"unexpected AniList API status {status}")
        decoded = json.loads(body)
        if decoded.get("errors"):
            raise ValueError("AniList returned a GraphQL error")
        media = decoded.get("data", {}).get("Page", {}).get("media", [])
        now = datetime.now(UTC)
        result: list[Article] = []
        for item in media:
            episode = item.get("nextAiringEpisode")
            if not episode:
                continue
            airing_at = datetime.fromtimestamp(int(episode["airingAt"]), UTC)
            if airing_at < since or airing_at > now + timedelta(hours=self.config.settings.lookback_hours):
                continue
            titles = item.get("title") or {}
            title = titles.get("english") or titles.get("romaji") or titles.get("native")
            if not title:
                continue
            link = item.get("siteUrl") or f"https://anilist.co/anime/{item['id']}"
            excerpt = f"Episode {episode.get('episode', '?')} airs at {airing_at.isoformat()} (local time shown in the briefing)."
            result.append(
                Article(
                    id=stable_id(f"{source.id}\n{item['id']}\n{episode['episode']}"),
                    topic_id=source.topic,
                    source_id=source.id,
                    source_name=source.name,
                    source_host="anilist.co",
                    trust=source.trust,
                    title=f"Upcoming anime episode: {title}",
                    link=normalize_url(link),
                    published_at=airing_at,
                    excerpt=excerpt,
                )
            )
        self.store.save_articles(result)
        return FetchResult(result)

    async def fetch_ical(self, source: SourceConfig, since: datetime) -> FetchResult:
        headers = self.store.request_headers(source.id)
        status, response_headers, body, final_url = await self.get_bytes(source.url, headers)
        if status == 304:
            return FetchResult(self.store.cached_articles(source.id, since), unchanged=True)
        self.store.set_response_cache(
            source.id,
            response_headers.get("etag"),
            response_headers.get("last-modified"),
        )
        result = parse_ical_events(body, source, datetime.now(UTC), self.config.settings.local_timezone)
        self.store.save_articles(result)
        return FetchResult(result)

    async def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self.robots_cache:
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            parser = RobotFileParser(robots_url)
            try:
                status, _, body, _ = await self.get_bytes(robots_url, {}, max_bytes=256_000)
                if status == 200:
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
                elif status >= 500:
                    return False
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500 or exc.response.status_code in (401, 403):
                    return False
                parser.parse([])  # robots.txt not found: RFC robots semantics allow crawling
            except (httpx.HTTPError, ValueError):
                return False
            self.robots_cache[host] = parser
        return self.robots_cache[host].can_fetch("MorningBar", url)

    async def fetch_page(self, source: SourceConfig, since: datetime) -> FetchResult:
        if not await self.robots_allowed(source.url):
            raise PermissionError("robots.txt disallows this page or is unavailable")
        headers = self.store.request_headers(source.id)
        status, response_headers, body, final_url = await self.get_bytes(source.url, headers)
        if status == 304:
            return FetchResult([], unchanged=True)
        content_type = response_headers.get("content-type", "").lower()
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            raise ValueError("page watcher received a non-HTML response")
        self.store.set_response_cache(
            source.id,
            response_headers.get("etag"),
            response_headers.get("last-modified"),
        )
        text = trafilatura.extract(body, include_comments=False, include_tables=False)
        if not text:
            raise ValueError("could not extract readable public page text")
        current_excerpt = clean_text(text, 1500)
        current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = self.store.page_snapshot(source.id)
        self.store.set_page_snapshot(source.id, current_hash, current_excerpt)
        if previous is None or previous[0] == current_hash:
            return FetchResult([])
        diff_lines = list(
            difflib.unified_diff(
                previous[1].splitlines(), current_excerpt.splitlines(),
                fromfile="previous public page excerpt", tofile="current public page excerpt",
                lineterm="",
            )
        )
        diff_excerpt = clean_text(" ".join(diff_lines), 600)
        if not diff_excerpt:
            diff_excerpt = "Page content changed; open the official page to review the current text."
        article = Article(
            id=stable_id(f"{source.id}\n{current_hash}"),
            topic_id=source.topic,
            source_id=source.id,
            source_name=source.name,
            source_host=(urlsplit(final_url).hostname or "").lower(),
            trust=source.trust,
            title=f"Official page changed: {source.name}",
            link=normalize_url(final_url),
            published_at=datetime.now(UTC),
            excerpt=diff_excerpt,
        )
        self.store.save_articles([article])
        return FetchResult([article])


def content_value(entry: Any) -> str:
    content = entry.get("content") or []
    if content and isinstance(content[0], dict):
        return str(content[0].get("value", ""))
    return ""


def parse_feed_bytes(
    body: bytes,
    source: SourceConfig,
    *,
    max_items: int,
    fetched_at: datetime,
    final_url: str,
) -> list[Article]:
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError("feed did not contain valid RSS or Atom entries")
    host = (urlsplit(final_url).hostname or "").lower()
    items: list[Article] = []
    for entry in parsed.entries[:max_items]:
        title = clean_text(entry.get("title", ""), 300)
        link = entry.get("link") or entry.get("id") or ""
        if not title or not link:
            continue
        link = urljoin(final_url, link)
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = struct_to_datetime(published) if published else fetched_at
        excerpt = clean_text(
            entry.get("summary") or entry.get("description") or content_value(entry), 600
        )
        normalized_link = normalize_url(link)
        items.append(
            Article(
                id=stable_id(f"{source.id}\n{normalized_link}"),
                topic_id=source.topic,
                source_id=source.id,
                source_name=source.name,
                source_host=host,
                trust=source.trust,
                title=title,
                link=normalized_link,
                published_at=published_at,
                excerpt=excerpt,
            )
        )
    return items


def parse_huggingface_bytes(
    body: bytes,
    source: SourceConfig,
    *,
    since: datetime,
    max_items: int,
    fetched_at: datetime,
    final_url: str,
    keywords: list[str],
) -> list[Article]:
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise ValueError("Hugging Face Daily Papers response was not a list")
    host = (urlsplit(final_url).hostname or "").lower()
    relevant_terms = [word.casefold() for word in keywords]
    result: list[Article] = []
    for entry in payload[:max_items]:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper", entry)
        if not isinstance(paper, dict):
            continue
        title = clean_text(paper.get("title", ""), 300)
        summary = clean_text(paper.get("summary", ""), 600)
        paper_id = clean_text(paper.get("id", ""), 80)
        if not title or not paper_id:
            continue
        published_text = paper.get("publishedAt") or entry.get("publishedAt")
        try:
            published_at = datetime.fromisoformat(
                str(published_text).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError):
            published_at = fetched_at
        if published_at < since:
            continue
        search_text = f"{title} {summary}".casefold()
        if relevant_terms and not any(term in search_text for term in relevant_terms):
            continue
        link = normalize_url(f"https://huggingface.co/papers/{paper_id}")
        result.append(
            Article(
                id=stable_id(f"{source.id}\n{paper_id}"),
                topic_id=source.topic,
                source_id=source.id,
                source_name=source.name,
                source_host=host,
                trust=source.trust,
                title=title,
                link=link,
                published_at=published_at,
                excerpt=summary,
            )
        )
    return result


def parse_ical_events(
    body: bytes,
    source: SourceConfig,
    now: datetime,
    local_timezone: str,
) -> list[Article]:
    try:
        local_zone = ZoneInfo(local_timezone)
    except ZoneInfoNotFoundError:
        local_zone = UTC
    floating_zone_name = str(source.params.get("floating_timezone", "America/New_York"))
    try:
        floating_zone = ZoneInfo(floating_zone_name)
    except ZoneInfoNotFoundError:
        floating_zone = local_zone
    cal = Calendar.from_ical(body)
    today = now.astimezone(local_zone).date()
    now_local = now.astimezone(local_zone)
    keywords = source.params.get(
        "keywords",
        ["consumer price index", "employment situation", "nonfarm payroll", "fomc", "inflation"],
    )
    if isinstance(keywords, str):
        keywords = [keywords]
    result: list[Article] = []
    for event in cal.walk("VEVENT"):
        summary = clean_text(str(event.get("summary", "")), 300)
        start_property = event.get("dtstart")
        if not summary or start_property is None:
            continue
        start_value = start_property.dt
        if isinstance(start_value, date) and not isinstance(start_value, datetime):
            start = datetime.combine(start_value, time.min, tzinfo=local_zone)
        elif start_value.tzinfo is None:
            start = start_value.replace(tzinfo=floating_zone)
        else:
            start = start_value
        start_local = start.astimezone(local_zone)
        if start_local.date() != today or start_local < now_local:
            continue
        description = clean_text(str(event.get("description", "")), 600)
        search_text = f"{summary} {description}".casefold()
        if not any(str(keyword).casefold() in search_text for keyword in keywords):
            continue
        uid = str(event.get("uid", summary))
        result.append(
            Article(
                id=stable_id(f"{source.id}\n{uid}\n{start.astimezone(UTC).isoformat()}"),
                topic_id=source.topic,
                source_id=source.id,
                source_name=source.name,
                source_host=(urlsplit(source.url).hostname or "").lower(),
                trust=source.trust,
                title=f"Today: {summary}",
                link=normalize_url(source.url),
                published_at=start.astimezone(UTC),
                excerpt=(description + " · " if description else "")
                + f"Scheduled for {start_local.strftime('%H:%M %Z')} local time.",
            )
        )
    return result


def clean_text(value: str, limit: int) -> str:
    value = html.unescape(str(value))
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def struct_to_datetime(value: struct_time) -> datetime:
    return datetime(*value[:6], tzinfo=UTC)


def _manual_intervention(article: Article) -> bool:
    text = f"{article.title} {article.excerpt}".casefold()
    markers = (
        "manual intervention",
        "manual upgrade",
        "manual action required",
        "requires manual",
        "manual steps required",
    )
    return any(marker in text for marker in markers)


def _article_score(article: Article, topic: TopicConfig, now: datetime, corroboration: int) -> float:
    age_hours = max(0.0, (now - article.published_at).total_seconds() / 3600)
    freshness = max(0.0, 3.0 - age_hours / 12.0)
    text = f"{article.title} {article.excerpt}".casefold()
    keyword_score = 0.0
    for keyword in topic.keywords:
        if keyword.casefold() in text:
            keyword_score += 0.25
    keyword_score += sum(
        boost for keyword, boost in topic.keyword_boosts.items() if keyword.casefold() in text
    )
    return topic.weight * 2.0 + article.trust + freshness + keyword_score + max(0, corroboration - 1) * 1.2


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[\w]+", title.casefold()))


def cluster_and_rank(
    articles: list[Article], topics: dict[str, TopicConfig], now: datetime | None = None
) -> list[Story]:
    now = now or datetime.now(UTC)
    ordered = sorted(articles, key=lambda article: article.published_at, reverse=True)
    clusters: list[list[Article]] = []
    keys: dict[str, int] = {}
    for article in ordered:
        normalized = normalize_url(article.link)
        if normalized in keys:
            clusters[keys[normalized]].append(article)
            continue
        match_index: int | None = None
        for index, group in enumerate(clusters):
            exemplar = group[0]
            if abs((exemplar.published_at - article.published_at).total_seconds()) > 36 * 3600:
                continue
            if exemplar.topic_id != article.topic_id and topics[exemplar.topic_id].weight != topics[article.topic_id].weight:
                continue
            if fuzz.token_set_ratio(_title_key(exemplar.title), _title_key(article.title)) >= 92:
                match_index = index
                break
        if match_index is None:
            match_index = len(clusters)
            clusters.append([article])
        else:
            clusters[match_index].append(article)
        keys[normalized] = match_index

    stories: list[Story] = []
    for group in clusters:
        source_unique: dict[str, Article] = {}
        for item in group:
            source_unique.setdefault(item.source_id, item)
        distinct = list(source_unique.values())
        leader = max(
            distinct,
            key=lambda item: _article_score(item, topics[item.topic_id], now, len(distinct)),
        )
        topic = topics[leader.topic_id]
        effective_outlets = len({item.source_host for item in distinct})
        manual = topic.manual_priority and any(_manual_intervention(item) for item in distinct)
        score = _article_score(leader, topic, now, max(1, effective_outlets))
        if manual:
            score += 1000.0
        canonical_key = _title_key(leader.title)
        stories.append(
            Story(
                id=stable_id(f"{leader.topic_id}\n{canonical_key}"),
                topic_id=leader.topic_id,
                topic_label=topic.label,
                title=leader.title,
                link=leader.link,
                source_names=sorted({item.source_name for item in distinct}),
                published_at=max(item.published_at for item in distinct),
                excerpt=leader.excerpt,
                score=score,
                outlet_count=effective_outlets,
                manual_priority=manual,
            )
        )
    return sorted(stories, key=lambda story: (story.manual_priority, story.score), reverse=True)


def filter_shown_stories(stories: list[Story], shown_ids: set[str]) -> list[Story]:
    """Retain Arch manual-action items even when they appeared in a recent briefing."""
    return [story for story in stories if story.manual_priority or story.id not in shown_ids]

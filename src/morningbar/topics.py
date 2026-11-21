from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import stat
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
import trafilatura
from icalendar import Calendar
from .models import InterestsConfig, SourceConfig, TopicConfig

MAX_SOURCE_BYTES = 1_000_000
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class SourceCheck:
    source: SourceConfig
    detail: str
    ok: bool


def slug_id(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_value.lower()).strip("_")[:60].rstrip("_")


async def verify_sources(sources: list[SourceConfig]) -> list[SourceCheck]:
    """Fetch and parse proposed public sources before presenting them for approval."""
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": "MorningBar/0.1 (personal local news reader)",
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/html;q=0.9, text/calendar;q=0.8, */*;q=0.5",
        },
    ) as client:
        semaphore = asyncio.Semaphore(3)

        async def one(source: SourceConfig) -> SourceCheck:
            async with semaphore:
                try:
                    status, headers, body, _ = await _get_public(
                        client, source.url, check_robots=source.kind == "page"
                    )
                    content_type = headers.get("content-type", "").lower()
                    if source.kind == "feed":
                        parsed = feedparser.parse(body)
                        if not parsed.version:
                            raise ValueError("response is not a parseable RSS or Atom feed")
                        detail = f"HTTP {status}; {parsed.version} feed"
                        if parsed.entries:
                            detail += f", {len(parsed.entries)} entries"
                    elif source.kind == "ical":
                        if "calendar" not in content_type and b"BEGIN:VCALENDAR" not in body[:4096]:
                            raise ValueError("response is not an iCalendar feed")
                        calendar = Calendar.from_ical(body)
                        count = sum(1 for component in calendar.walk() if component.name == "VEVENT")
                        detail = f"HTTP {status}; iCalendar, {count} events"
                    else:
                        if "text/html" not in content_type and "application/xhtml" not in content_type:
                            raise ValueError("page did not return HTML")
                        if not trafilatura.extract(body, include_comments=False, include_tables=False):
                            raise ValueError("page has no readable public text")
                        detail = f"HTTP {status}; readable HTML"
                    return SourceCheck(source, detail, True)
                except Exception as exc:
                    return SourceCheck(source, f"unavailable ({type(exc).__name__})", False)

        return await asyncio.gather(*(one(source) for source in sources))


async def _check_robots(client: httpx.AsyncClient, url: str) -> None:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    status, _, body, _ = await _get_public(client, robots_url, allow_error_status=True)
    if status in (401, 403) or status >= 500:
        raise PermissionError("robots.txt is unavailable or denies access")
    parser = RobotFileParser()
    if status == 200:
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
    else:
        parser.parse([])
    if not parser.can_fetch("MorningBar", url):
        raise PermissionError("robots.txt disallows this page")


async def _get_public(
    client: httpx.AsyncClient,
    url: str,
    *,
    allow_error_status: bool = False,
    check_robots: bool = False,
) -> tuple[int, httpx.Headers, bytes, str]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("source must use a public HTTPS URL without credentials")
        await _assert_public_host(parts.hostname)
        if check_robots:
            await _check_robots(client, current)
        async with client.stream("GET", current) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect response has no destination")
                current = urljoin(str(response.url), location)
                continue
            if not allow_error_status:
                response.raise_for_status()
            elif response.status_code not in {200, 404, 410}:
                response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_SOURCE_BYTES:
                raise ValueError("source response is too large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise ValueError("source response is too large")
                chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks), current
    raise ValueError("source redirected too many times")


async def _assert_public_host(host: str) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        records = await asyncio.to_thread(
            socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
        )
        addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    else:
        addresses = {literal}
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("source host does not resolve only to public addresses")


def build_topic_and_sources(label: str, proposal, existing: InterestsConfig) -> tuple[TopicConfig, list[SourceConfig]]:
    topic_id = slug_id(label)
    if not topic_id:
        raise ValueError("topic label needs at least one ASCII letter or number")
    if any(topic.id == topic_id for topic in existing.topics):
        raise ValueError(f"topic id {topic_id!r} already exists")
    topic = TopicConfig(
        id=topic_id,
        label=label.strip(),
        weight=proposal.weight,
        keywords=proposal.keywords,
        keyword_boosts=proposal.keyword_boosts,
        manual_priority=proposal.manual_priority,
        news_only=proposal.news_only,
    )
    used_source_ids = {source.id for source in existing.sources}
    existing_urls = {source.url.rstrip("/") for source in existing.sources}
    sources: list[SourceConfig] = []
    for candidate in proposal.sources:
        url = str(candidate.url)
        if url.rstrip("/") in existing_urls:
            continue
        root = f"{topic_id}_{slug_id(candidate.name) or 'source'}"[:80].rstrip("_")
        source_id = root
        suffix = 2
        while source_id in used_source_ids:
            source_id = f"{root[:75]}_{suffix}"
            suffix += 1
        source = SourceConfig(
            id=source_id,
            topic=topic_id,
            name=candidate.name,
            url=url,
            kind=candidate.kind,
            trust=candidate.trust,
            max_items=candidate.max_items,
        )
        sources.append(source)
        used_source_ids.add(source_id)
        existing_urls.add(url.rstrip("/"))
    if not sources:
        raise ValueError("Gemini proposed no new sources")
    return topic, sources


def append_topic_to_config(path: Path, topic: TopicConfig, sources: list[SourceConfig]) -> None:
    """Append an approved proposal atomically while preserving the existing TOML."""
    resolved = path.expanduser().resolve(strict=True)
    original = resolved.read_text(encoding="utf-8")
    existing = InterestsConfig.model_validate(tomllib.loads(original))
    updated = existing.model_copy(update={
        "topics": [*existing.topics, topic],
        "sources": [*existing.sources, *sources],
    })
    # Validate references and IDs through the model's post-validator before writing.
    updated = InterestsConfig.model_validate(updated.model_dump(mode="python"))
    content = original.rstrip() + "\n\n" + _topic_toml(topic, sources) + "\n"
    parsed = InterestsConfig.model_validate(tomllib.loads(content))
    if parsed.topics[-1].id != topic.id:
        raise ValueError("appended topic could not be validated")

    metadata = resolved.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        if resolved.read_text(encoding="utf-8") != original:
            raise RuntimeError("config changed while the proposal was being approved; no update was written")
        os.replace(temp_name, resolved)
        directory_fd = os.open(resolved.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _topic_toml(topic: TopicConfig, sources: list[SourceConfig]) -> str:
    lines = [
        "# Added with morningbar topic add after source verification and approval.",
        "[[topics]]",
        f"id = {_quote(topic.id)}",
        f"label = {_quote(topic.label)}",
        f"weight = {topic.weight}",
        f"keywords = {_array(topic.keywords)}",
        f"keyword_boosts = {_mapping(topic.keyword_boosts)}",
        f"manual_priority = {str(topic.manual_priority).lower()}",
        f"news_only = {str(topic.news_only).lower()}",
        f"watchlist = {_array(topic.watchlist)}",
    ]
    for source in sources:
        lines.extend([
            "",
            "[[sources]]",
            f"id = {_quote(source.id)}",
            f"topic = {_quote(source.topic)}",
            f"name = {_quote(source.name)}",
            f"url = {_quote(source.url)}",
            f"kind = {_quote(source.kind)}",
            f"trust = {source.trust}",
            f"max_items = {source.max_items}",
        ])
        if source.params:
            lines.append(f"params = {_mapping(source.params)}")
    return "\n".join(lines)


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _array(values: list[str]) -> str:
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _mapping(values: dict) -> str:
    pairs = [f"{_quote(str(key))} = {_toml_value(value)}" for key, value in values.items()]
    return "{ " + ", ".join(pairs) + " }" if pairs else "{}"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")

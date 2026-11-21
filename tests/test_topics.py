from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from morningbar.cli import app
from morningbar.config import load_config
from morningbar.gemini import ProposedSource, TopicProposalResponse
from morningbar.models import SourceConfig, TopicConfig
from morningbar.paths import Paths
from morningbar.topics import (
    SourceCheck,
    append_topic_to_config,
    build_topic_and_sources,
    slug_id,
    verify_sources,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_slug_id_is_stable_and_config_safe() -> None:
    assert slug_id("  AI & Robotics! ") == "ai_robotics"
    assert slug_id("中文") == ""


def test_build_topic_drops_duplicate_urls_and_generates_unique_source_ids() -> None:
    existing = load_config(FIXTURES / "interests.toml")
    proposal = TopicProposalResponse(
        weight=4,
        keywords=["robotics", "robot arms", "autonomous robots"],
        keyword_boosts={"robot arms": 1.5},
        sources=[
            ProposedSource(name="Robotics News", url="https://robotics.example/feed.xml"),
            ProposedSource(name="Duplicate", url="https://robotics.example/feed.xml"),
            ProposedSource(name="Robotics News", url="https://robotics.example/other.xml"),
        ],
    )

    topic, sources = build_topic_and_sources("AI Robotics", proposal, existing)

    assert topic.id == "ai_robotics"
    assert topic.weight == 4
    assert len(sources) == 2
    assert sources[0].id != sources[1].id
    assert all(source.topic == topic.id for source in sources)


def test_build_topic_rejects_existing_topic_id() -> None:
    existing = load_config(FIXTURES / "interests.toml")
    proposal = TopicProposalResponse(
        weight=3,
        keywords=["AI", "machine learning", "neural networks"],
        sources=[ProposedSource(name="AI feed", url="https://ai.example/feed.xml")],
    )

    with pytest.raises(ValueError, match="already exists"):
        build_topic_and_sources("AI research", proposal, existing)


def test_append_topic_preserves_existing_toml_and_writes_valid_config(tmp_path: Path) -> None:
    config_path = tmp_path / "interests.toml"
    original = (FIXTURES / "interests.toml").read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")
    config_path.chmod(0o640)
    topic = TopicConfig(
        id="robotics", label="Robotics", weight=4,
        keywords=["robotics", "robot arms", "autonomous robots"],
        keyword_boosts={"robot arms": 1.5},
    )
    source = SourceConfig(
        id="robotics_news", topic="robotics", name="Robotics News",
        url="https://robotics.example/feed.xml", kind="feed", trust=3,
    )

    append_topic_to_config(config_path, topic, [source])

    saved_text = config_path.read_text(encoding="utf-8")
    parsed = load_config(config_path)
    assert saved_text.startswith(original.rstrip())
    assert parsed.topics[-1] == topic
    assert parsed.sources[-1] == source
    assert config_path.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_verify_sources_parses_feed_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow_public_host(_host: str) -> None:
        return None

    async def fake_get(_client, url: str, **_kwargs):
        body = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <title>Robotics News</title><link>https://robotics.example/</link>
        <description>News</description><item><title>New arm</title>
        <link>https://robotics.example/arm</link></item>
        </channel></rss>"""
        return 200, httpx.Headers({"content-type": "application/rss+xml"}), body, url

    monkeypatch.setattr("morningbar.topics._assert_public_host", allow_public_host)
    monkeypatch.setattr("morningbar.topics._get_public", fake_get)
    source = SourceConfig(
        id="robotics_news", topic="robotics", name="Robotics News",
        url="https://robotics.example/feed.xml", kind="feed",
    )

    [result] = await verify_sources([source])

    assert result.ok
    assert "HTTP 200" in result.detail
    assert "rss20" in result.detail


@pytest.mark.asyncio
async def test_verify_sources_rejects_unparseable_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow_public_host(_host: str) -> None:
        return None

    async def fake_get(_client, url: str, **_kwargs):
        return 200, httpx.Headers({"content-type": "text/html"}), b"<html>wrong page</html>", url

    monkeypatch.setattr("morningbar.topics._assert_public_host", allow_public_host)
    monkeypatch.setattr("morningbar.topics._get_public", fake_get)
    source = SourceConfig(
        id="robotics_news", topic="robotics", name="Robotics News",
        url="https://robotics.example/feed.xml", kind="feed",
    )

    [result] = await verify_sources([source])

    assert not result.ok
    assert result.detail == "unavailable (ValueError)"


@pytest.mark.asyncio
async def test_verify_page_checks_robots_before_fetching_page(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    async def allow_public_host(_host: str) -> None:
        return None

    class FakeResponse:
        def __init__(self, url: str):
            self.url = httpx.URL(url)
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "text/plain"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield b"User-agent: *\nDisallow: /private\n"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method: str, url: str):
            requested.append(url)
            if not url.endswith("/robots.txt"):
                raise AssertionError("robots-disallowed page must not be fetched")
            return FakeResponse(url)

    monkeypatch.setattr("morningbar.topics._assert_public_host", allow_public_host)
    monkeypatch.setattr("morningbar.topics.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    source = SourceConfig(
        id="robotics_page", topic="robotics", name="Robotics page",
        url="https://robotics.example/private/news", kind="page",
    )

    [result] = await verify_sources([source])

    assert not result.ok
    assert result.detail == "unavailable (PermissionError)"
    assert requested == ["https://robotics.example/robots.txt"]


@pytest.mark.parametrize("approval", ["n", "y"])
def test_topic_command_only_writes_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approval: str
) -> None:
    config_path = tmp_path / "interests.toml"
    original = (FIXTURES / "interests.toml").read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")
    paths = Paths(tmp_path / "app-config", tmp_path / "state")
    monkeypatch.setattr("morningbar.cli.Paths.current", classmethod(lambda _cls: paths))
    monkeypatch.setattr("morningbar.cli.resolve_api_key", lambda _path: "test-key")

    proposal = TopicProposalResponse(
        weight=4,
        keywords=["robotics", "robot arms", "autonomous robots"],
        sources=[ProposedSource(name="Robotics News", url="https://robotics.example/feed.xml")],
    )

    class FakeProposer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def propose_topic(self, _label: str):
            return proposal

    monkeypatch.setattr("morningbar.cli.GeminiRanker", FakeProposer)

    async def verified(sources: list[SourceConfig]) -> list[SourceCheck]:
        return [SourceCheck(source, "HTTP 200; rss20 feed", True) for source in sources]

    monkeypatch.setattr("morningbar.cli.verify_sources", verified)
    result = CliRunner().invoke(
        app,
        ["topic", "add", "Robotics", "--config", str(config_path)],
        input=f"{approval}\n",
    )

    assert result.exit_code == 0, result.output
    parsed = load_config(config_path)
    if approval == "y":
        assert parsed.topics[-1].id == "robotics"
        assert parsed.sources[-1].topic == "robotics"
        assert "Added Robotics" in result.output
    else:
        assert config_path.read_text(encoding="utf-8") == original
        assert "No changes made." in result.output

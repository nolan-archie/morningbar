import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from morningbar.config import load_config
from morningbar.gemini import GeminiRanker, resolve_api_key
from morningbar.models import Article, GeminiSettings, Story
from morningbar.status import StatusWriter
from morningbar.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"


def test_usage_reservations_enforce_daily_caps_and_record_actual_tokens(tmp_path: Path) -> None:
    store = Store(tmp_path / "usage.sqlite3")
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)

    call_id, reason = store.reserve_gemini_call(
        "gemini-test", 100, 200,
        max_requests_per_minute=5, max_requests_per_day=1,
        max_tokens_per_minute=2_000, max_tokens_per_day=2_000,
        now=now,
    )
    assert call_id is not None and reason is None
    store.finish_gemini_call(call_id, 80, 90)
    blocked_id, blocked_reason = store.reserve_gemini_call(
        "gemini-test", 1, 1,
        max_requests_per_minute=5, max_requests_per_day=1,
        max_tokens_per_minute=2_000, max_tokens_per_day=2_000,
        now=now,
    )

    assert blocked_id is None
    assert blocked_reason == "daily request cap reached"
    assert store.gemini_usage_today(now) == {
        "date": store.gemini_usage_date(now), "requests": 1, "tokens": 170,
    }


def test_feedback_adjustments_are_gentle_and_bounded(tmp_path: Path) -> None:
    store = Store(tmp_path / "feedback.sqlite3")
    for index in range(20):
        store.save_feedback(f"up-{index}", "ai_research", vote=1)
    store.save_feedback("down", "linux", vote=-1)
    store.save_feedback("less", "linux", less_like=True)
    store.save_feedback("up-and-less", "linux", vote=1)
    store.save_feedback("up-and-less", "linux", less_like=True)

    adjustments = store.topic_score_adjustments()
    assert adjustments["ai_research"] == 2.0
    assert adjustments["linux"] == -1.0


def test_api_key_resolution_requires_mode_600_for_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "gemini-api-key"
    key_file.write_text("local-secret\n", encoding="utf-8")
    key_file.chmod(0o644)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("morningbar.gemini.shutil.which", lambda _: None)

    assert resolve_api_key(key_file) is None
    key_file.chmod(0o600)
    assert resolve_api_key(key_file) == "local-secret"
    monkeypatch.setenv("GEMINI_API_KEY", "environment-secret")
    assert resolve_api_key(key_file) == "environment-secret"


@pytest.mark.asyncio
async def test_gemini_ranker_uses_json_schema_and_stateless_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topics = load_config(FIXTURES / "interests.toml").topics
    article = Article(
        id="article-one", topic_id="ai_research", source_id="paper",
        source_name="Research Feed", source_host="example.test", trust=4,
        title="A compact KV cache for smaller models",
        link="https://example.test/kv-cache",
        published_at=datetime(2026, 11, 21, 8, tzinfo=UTC),
        excerpt="The method reduces memory use during model decoding.",
    )
    story = Story(
        id="story-one", topic_id="ai_research", topic_label="AI research",
        title=article.title, link=article.link, source_names=[article.source_name],
        published_at=article.published_at, excerpt=article.excerpt, score=12.0,
        outlet_count=1,
    )
    calls: list[dict] = []

    class FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "Public articles to rank:" in kwargs["input"]:
                records = json.loads(kwargs["input"].split("Public articles to rank:\n", 1)[1])
                output = {"items": [
                    {
                        "id": record["id"],
                        "importance": 8,
                        "category_id": record["category_id"],
                        "summary": "The approach cuts memory use for model decoding.",
                        "why_it_matters": "It matches your interest in efficient models.",
                    }
                    for record in records
                ]}
            else:
                output = {"headline": "A new route to smaller model inference.", "tldr": ["KV-cache memory use falls."]}
            return SimpleNamespace(
                output_text=json.dumps(output),
                usage=SimpleNamespace(total_input_tokens=120, total_output_tokens=90),
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.interactions = FakeInteractions()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("morningbar.gemini.genai.Client", FakeClient)
    settings = GeminiSettings(minimum_request_interval_seconds=1)
    store = Store(tmp_path / "ranker.sqlite3")
    ranker = GeminiRanker(
        settings, tmp_path / "missing-key", topics, store,
        StatusWriter(tmp_path / "status.json"),
    )

    result = await ranker.create_briefing([story])

    assert result.reason is None
    assert result.rankings[story.id].importance == 8
    assert result.digest is not None
    assert [call["store"] for call in calls] == [False, False]
    assert all(call["response_format"]["mime_type"] == "application/json" for call in calls)
    assert store.gemini_usage_today()["requests"] == 2

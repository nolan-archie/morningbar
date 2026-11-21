from datetime import UTC, datetime
from pathlib import Path

from morningbar.briefing import BriefingStory, select_final
from morningbar.config import load_config

PROJECT_CONFIG = Path(__file__).parents[1] / "config" / "interests.proposal.toml"


def _story(story_id: str, category_id: str, importance: int, *, manual: bool = False) -> BriefingStory:
    return BriefingStory(
        id=story_id,
        category_id=category_id,
        category=category_id,
        title=story_id,
        link=f"https://example.test/{story_id}",
        source_names=["Fixture"],
        published_at=datetime(2026, 11, 21, tzinfo=UTC),
        summary="Fixture summary.",
        why_it_matters="Fixture relevance.",
        importance=importance,
        local_score=float(importance),
        outlet_count=1,
        manual_priority=manual,
    )


def test_final_selection_applies_category_cap_and_manual_priority() -> None:
    stories = [
        _story("linux-action", "linux", 2, manual=True),
        _story("linux-1", "linux", 10),
        _story("linux-2", "linux", 9),
        _story("linux-3", "linux", 8),
        _story("ai-1", "ai_research", 6),
    ]

    selected = select_final(stories, max_stories=4, max_per_category=2)

    assert [item.id for item in selected] == ["linux-action", "linux-1", "ai-1"]


def test_example_config_has_current_model_defaults_and_approved_game_topics() -> None:
    config = load_config(PROJECT_CONFIG)

    assert config.gemini.primary_model == "gemini-3.8-flash"
    assert config.gemini.fallback_model == "gemini-3.5-flash-lite"
    games = next(topic for topic in config.topics if topic.id == "games")
    assert all("wuthering" not in keyword.casefold() for keyword in games.keywords)
    assert all("neverness" not in keyword.casefold() for keyword in games.keywords)
    assert len(config.sources) == 65

from datetime import UTC, datetime, timedelta
from pathlib import Path

from morningbar.collector import (
    RunBudgetExceeded,
    RunByteBudget,
    cluster_and_rank,
    filter_shown_stories,
    parse_feed_bytes,
    parse_huggingface_bytes,
    parse_ical_events,
)
from morningbar.config import load_config
from morningbar.models import Article, SourceConfig
from morningbar.urls import normalize_url

FIXTURES = Path(__file__).parent / "fixtures"


async def test_run_body_budget_enforces_small_total_limit() -> None:
    budget = RunByteBudget(32)

    assert await budget.chunk_size() == 32
    await budget.consume(24)
    assert await budget.chunk_size() == 8
    await budget.consume(8)
    try:
        await budget.chunk_size()
    except RunBudgetExceeded:
        pass
    else:
        raise AssertionError("expected the exhausted byte budget to stop reads")


def test_saved_atom_fixture_is_parsed_and_tracking_params_removed() -> None:
    source = SourceConfig(
        id="papers",
        topic="ai_research",
        name="Research feed",
        url="https://example.test/feed.xml",
        trust=4,
    )
    content = (FIXTURES / "atom_feed.xml").read_bytes()
    items = parse_feed_bytes(
        content,
        source,
        max_items=20,
        fetched_at=datetime(2026, 11, 21, 10, tzinfo=UTC),
        final_url=source.url,
    )

    assert len(items) == 2
    assert items[0].title == "Low-memory inference with a compact KV cache"
    assert items[0].link == "https://example.test/papers/kv-cache"
    assert items[0].published_at == datetime(2026, 11, 21, 8, 30, tzinfo=UTC)
    assert "long-context decoding" in items[0].excerpt
    assert "generation speed" in items[1].excerpt


def test_url_normalization_strips_tracking_and_fragment() -> None:
    assert normalize_url(
        "HTTPS://Example.test:443/story/?utm_campaign=daily&id=4&fbclid=xyz#top"
    ) == "https://example.test/story?id=4"


def test_saved_huggingface_json_fixture_keeps_relevant_recent_paper() -> None:
    source = SourceConfig(
        id="hf_papers",
        topic="ai_research",
        name="Hugging Face Daily Papers",
        url="https://huggingface.co/api/daily_papers?p=0&limit=50",
        kind="huggingface",
        trust=4,
    )
    items = parse_huggingface_bytes(
        (FIXTURES / "huggingface_papers.json").read_bytes(),
        source,
        since=datetime(2026, 11, 21, 0, tzinfo=UTC),
        max_items=50,
        fetched_at=datetime(2026, 11, 21, 10, tzinfo=UTC),
        final_url=source.url,
        keywords=["quantization", "KV cache", "associative memory"],
    )

    assert len(items) == 1
    assert items[0].title == "Low-memory inference with quantized KV cache"
    assert items[0].link == "https://huggingface.co/papers/2611.00001"
    assert "long-context model decoding" in items[0].excerpt


def test_cross_source_story_clusters_and_corroboration_increases_score() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    first = Article(
        id="one", topic_id="ai_research", source_id="paper_a", source_name="Paper A",
        source_host="arxiv.org", trust=4, title="Memory efficient long-context decoding",
        link="https://arxiv.org/abs/1234.1", published_at=now - timedelta(hours=1),
        excerpt="A new KV cache approach.",
    )
    second = Article(
        id="two", topic_id="ai_research", source_id="paper_b", source_name="Paper B",
        source_host="huggingface.co", trust=3, title="Memory-efficient long context decoding",
        link="https://huggingface.co/papers/1234.1", published_at=now - timedelta(minutes=50),
        excerpt="A new KV cache approach.",
    )
    topics = load_config(FIXTURES / "interests.toml").topics
    ranked_topics = {topic.id: topic for topic in topics}

    single = cluster_and_rank([first], ranked_topics, now)[0]
    clustered = cluster_and_rank([first, second], ranked_topics, now)

    assert len(clustered) == 1
    assert clustered[0].outlet_count == 2
    assert set(clustered[0].source_names) == {"Paper A", "Paper B"}
    assert clustered[0].score > single.score


def test_arch_manual_intervention_is_forced_to_top() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    ordinary = Article(
        id="ordinary", topic_id="linux", source_id="news", source_name="Arch news",
        source_host="archlinux.org", trust=5, title="New package available",
        link="https://archlinux.org/news/new-package/", published_at=now,
    )
    action = Article(
        id="action", topic_id="linux", source_id="news", source_name="Arch news",
        source_host="archlinux.org", trust=5, title="Manual intervention required: rebuild packages",
        link="https://archlinux.org/news/manual-action/", published_at=now - timedelta(hours=1),
    )
    topics = load_config(FIXTURES / "interests.toml").topics
    ranked_topics = {topic.id: topic for topic in topics}

    result = cluster_and_rank([ordinary, action], ranked_topics, now)

    assert result[0].manual_priority is True
    assert result[0].title == action.title


def test_recently_shown_arch_manual_action_is_kept() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    ordinary = Article(
        id="ordinary", topic_id="linux", source_id="news", source_name="Arch news",
        source_host="archlinux.org", trust=5, title="Package update",
        link="https://archlinux.org/news/package-update/", published_at=now,
    )
    action = Article(
        id="action", topic_id="linux", source_id="news", source_name="Arch news",
        source_host="archlinux.org", trust=5,
        title="Manual intervention required for upgrade",
        link="https://archlinux.org/news/manual-action/", published_at=now,
    )
    topics = load_config(FIXTURES / "interests.toml").topics
    ranked_topics = {topic.id: topic for topic in topics}
    stories = cluster_and_rank([ordinary, action], ranked_topics, now)

    visible = filter_shown_stories(stories, {story.id for story in stories})

    assert len(visible) == 1
    assert visible[0].manual_priority is True


def test_example_interests_proposal_parses() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "interests.proposal.toml")
    assert len(config.topics) == 9
    assert len(config.sources) == 65
    assert next(topic for topic in config.topics if topic.id == "anime").watchlist == []
    games = next(topic for topic in config.topics if topic.id == "games")
    assert all("wuthering" not in keyword.casefold() for keyword in games.keywords)
    assert all("neverness" not in keyword.casefold() for keyword in games.keywords)
    assert not {"wuthering_waves_official", "neverness_official"} & {
        source.id for source in config.sources
    }


def test_calendar_keeps_only_high_impact_events_today_in_local_time() -> None:
    source = SourceConfig(
        id="bls_calendar", topic="ai_research", name="BLS calendar",
        url="https://www.bls.gov/schedule/bls.ics", kind="ical", trust=5,
    )
    events = parse_ical_events(
        (FIXTURES / "high_impact_calendar.ics").read_bytes(),
        source,
        datetime(2026, 11, 21, 12, tzinfo=UTC),
        "Africa/Johannesburg",
    )

    assert len(events) == 1
    assert "CPI" in events[0].title
    assert "16:30 SAST" in events[0].excerpt

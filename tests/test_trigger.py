import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from morningbar.collector import Collector
from morningbar.cli import _record_trigger_skip
from morningbar.models import Article, InterestsConfig, SourceConfig, TopicConfig
from morningbar.paths import Paths
from morningbar.status import StatusWriter
from morningbar.storage import Store
from morningbar.trigger import (
    NetworkState,
    TriggerLock,
    TriggerState,
    is_in_morning_window,
    probe_network,
    wait_for_network,
)


def test_morning_window_uses_configured_local_timezone_and_grace() -> None:
    start = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)  # 05:00 in Johannesburg
    end = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)  # 11:00 in Johannesburg

    assert is_in_morning_window(start, "Africa/Johannesburg", time(5), time(11))
    assert not is_in_morning_window(end, "Africa/Johannesburg", time(5), time(11))
    assert is_in_morning_window(end + timedelta(minutes=29), "Africa/Johannesburg", time(5), time(11), 30)
    assert not is_in_morning_window(end + timedelta(minutes=30), "Africa/Johannesburg", time(5), time(11), 30)


def test_trigger_state_records_local_day_and_debounce_time(tmp_path: Path) -> None:
    state = TriggerState(tmp_path / "trigger-state.json")
    now = datetime(2026, 11, 21, 10, tzinfo=UTC)
    state.mark_started(now)

    assert state.last_started() == now
    assert not state.completed_today("2026-11-21")
    state.mark_completed("2026-11-21", now + timedelta(seconds=20))
    assert state.completed_today("2026-11-21")
    assert not state.completed_today("2026-11-22")
    state.mark_outside_event("2026-11-21", now + timedelta(seconds=25))
    assert state.last_outside_event("2026-11-21") == now + timedelta(seconds=25)
    assert state.last_outside_event("2026-11-22") is None


def test_later_gated_event_keeps_today_briefing_ready(tmp_path: Path) -> None:
    day = "2026-11-21"
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.state_dir.mkdir()
    (paths.state_dir / f"briefing-{day}.json").write_text(
        '{"date":"2026-11-21","mode":"basic","stories":[{},{}]}', encoding="utf-8"
    )
    status = StatusWriter(paths.status_file)

    _record_trigger_skip(status, paths, day, "already-completed-today")

    payload = json.loads(paths.status_file.read_text(encoding="utf-8"))
    assert payload["stage"] == "ready"
    assert payload["progress"] == 2
    assert payload["trigger_gate"] == "already-completed-today"


def test_trigger_lock_rejects_second_process_lock(tmp_path: Path) -> None:
    first = TriggerLock(tmp_path / "trigger.lock")
    second = TriggerLock(tmp_path / "trigger.lock")
    first.__enter__()
    try:
        with pytest.raises(BlockingIOError):
            second.__enter__()
    finally:
        first.__exit__(None, None, None)


def test_probe_network_reads_connectivity_and_metered_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(("full", "wifi:connected: wlan0", "yes (guessed)"))
    monkeypatch.setattr("morningbar.trigger._run_nmcli", lambda _args: next(outputs))

    state = probe_network()

    assert state.connected
    assert state.metered
    assert state.active_devices == ("wlan0",)


def test_wait_for_network_uses_backoff_until_connected(tmp_path: Path) -> None:
    now = 10.0
    sleeps: list[float] = []
    responses = iter(
        (
            NetworkState(False, False, "none"),
            NetworkState(False, False, "none"),
            NetworkState(True, False, "full", ("wlan0",)),
        )
    )

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    settings = InterestsConfig(
        settings={"network_wait_seconds": 30},
        topics=[TopicConfig(id="ai", label="AI", weight=3)],
    ).settings
    result = wait_for_network(
        settings,
        StatusWriter(tmp_path / "status.json"),
        probe=lambda: next(responses),
        sleep=sleep,
        clock=lambda: now,
    )

    assert result.connected
    assert sleeps == [2.0, 4.0]


@pytest.mark.asyncio
async def test_offline_collection_uses_cache_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    source = SourceConfig(
        id="research", topic="ai", name="Research", url="https://example.test/feed.xml", trust=4
    )
    config = InterestsConfig(
        topics=[TopicConfig(id="ai", label="AI research", weight=5, keywords=["KV cache"])],
        sources=[source],
    )
    store = Store(tmp_path / "state.sqlite3")
    store.save_articles(
        [
            Article(
                id="cached-paper", topic_id="ai", source_id="research", source_name="Research",
                source_host="example.test", trust=4,
                title="A small KV cache paper", link="https://example.test/paper",
                published_at=now, excerpt="Memory-efficient inference.",
            )
        ]
    )

    async def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline collection attempted a network source")

    monkeypatch.setattr(Collector, "fetch_source", fail_if_called)
    stories, unavailable, total = await Collector(config, store, StatusWriter(tmp_path / "status.json")).collect(
        offline=True
    )

    assert len(stories) == 1
    assert stories[0].title == "A small KV cache paper"
    assert unavailable == 0
    assert total == 0

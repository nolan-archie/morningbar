from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from pydantic import ValidationError

from . import __version__
from .briefing import (
    BriefingDocument,
    BriefingStory,
    basic_digest,
    local_briefing_story,
    make_document,
    select_final,
)
from .collector import Collector
from .config import load_config
from .gemini import GeminiRanker, GeminiResult, resolve_api_key
from .models import Story
from .paths import Paths
from .status import StatusWriter
from .storage import Store
from .topics import append_topic_to_config, build_topic_and_sources, slug_id, verify_sources
from .trigger import TriggerLock, TriggerState, is_in_morning_window, wait_for_network

app = typer.Typer(no_args_is_help=True, help="Local morning news briefing")
topic_app = typer.Typer(no_args_is_help=True, help="Manage briefing topics")
app.add_typer(topic_app, name="topic")


@topic_app.command("add")
def topic_add(
    label: str = typer.Argument(..., help="Topic to propose, for example: robotics."),
    config: Optional[Path] = typer.Option(None, "--config", help="Interests TOML (defaults to XDG config)."),
) -> None:
    """Ask Gemini for keywords and sources, verify them, then review before saving."""
    clean_label = label.strip()
    if not clean_label or len(clean_label) > 80 or any(ord(char) < 32 for char in clean_label):
        typer.echo("Topic label must be 1–80 printable characters.", err=True)
        raise typer.Exit(code=2)
    topic_id = slug_id(clean_label)
    if not topic_id:
        typer.echo("Topic label needs at least one ASCII letter or number.", err=True)
        raise typer.Exit(code=2)

    paths = Paths.current()
    config_path = (config or paths.interests_file).expanduser()
    try:
        interests = load_config(config_path)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if any(topic.id == topic_id for topic in interests.topics):
        typer.echo(f"Topic id {topic_id!r} already exists; no changes were made.", err=True)
        raise typer.Exit(code=2)
    if not interests.gemini.enabled:
        typer.echo("Gemini is disabled in this interests config; no changes were made.", err=True)
        raise typer.Exit(code=1)
    if not resolve_api_key(paths.gemini_key_file):
        typer.echo("No Gemini API key is available; no changes were made.", err=True)
        raise typer.Exit(code=1)

    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(paths.database_file)
    proposer = GeminiRanker(
        interests.gemini,
        paths.gemini_key_file,
        interests.topics,
        store,
        None,
    )
    try:
        proposal = asyncio.run(proposer.propose_topic(clean_label))
    except Exception as exc:
        logging.getLogger(__name__).warning("topic proposal failed (%s)", type(exc).__name__)
        proposal = None
    if proposal is None:
        typer.echo("Gemini did not return a valid proposal; no changes were made.", err=True)
        raise typer.Exit(code=1)

    try:
        topic, sources = build_topic_and_sources(clean_label, proposal, interests)
    except (ValueError, ValidationError) as exc:
        typer.echo(f"Proposal could not be used: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Checking {len(sources)} proposed source URLs…")
    try:
        checks = asyncio.run(verify_sources(sources))
    except Exception as exc:
        logging.getLogger(__name__).warning("source verification failed (%s)", type(exc).__name__)
        typer.echo("Source verification failed; no changes were made.", err=True)
        raise typer.Exit(code=1) from exc
    verified = [check.source for check in checks if check.ok]
    for check in checks:
        state = check.detail if check.ok else f"excluded: {check.detail}"
        typer.echo(f"- {check.source.name}: {state}\n  {check.source.url}")
    typer.echo(f"\nTopic: {topic.label} (weight {topic.weight}/5, id {topic.id})")
    typer.echo("Keywords: " + ", ".join(topic.keywords))
    if topic.keyword_boosts:
        boosts = ", ".join(f"{key} +{value:g}" for key, value in topic.keyword_boosts.items())
        typer.echo("Keyword boosts: " + boosts)
    typer.echo(
        f"Flags: manual_priority={str(topic.manual_priority).lower()}, "
        f"news_only={str(topic.news_only).lower()}"
    )
    typer.echo(f"Verified sources to add: {len(verified)}")
    if not verified:
        typer.echo("No proposed sources passed verification; no changes were made.", err=True)
        raise typer.Exit(code=1)
    if not typer.confirm(f"Add this topic to {config_path}?", default=False):
        typer.echo("No changes made.")
        return
    try:
        append_topic_to_config(config_path, topic, verified)
    except (OSError, RuntimeError, ValueError, ValidationError) as exc:
        typer.echo(f"Could not update interests config: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Added {topic.label} and {len(verified)} verified source(s) to {config_path}.")


@app.command()
def run(
    no_llm: bool = typer.Option(False, "--no-llm", help="Use local ranking only."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Collect and preview without writing a briefing."),
    config: Optional[Path] = typer.Option(None, "--config", help="Interests TOML (defaults to XDG config)."),
) -> None:
    """Collect, rank and save a compact morning briefing."""
    paths = Paths.current()
    config_path = (config or paths.interests_file).expanduser()
    try:
        interests = load_config(config_path)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(paths.state_dir, 0o700)
    except OSError:
        pass
    store = Store(paths.database_file)
    status = StatusWriter(paths.status_file)
    status.update("starting", 0, len(interests.sources))
    try:
        document, stories, unavailable, total = asyncio.run(
            _run_pipeline(
                interests,
                store,
                status,
                paths,
                no_llm=no_llm,
                dry_run=dry_run,
            )
        )
    except KeyboardInterrupt:
        status.update("interrupted")
        raise typer.Exit(code=130)

    typer.echo(f"Morning Bar {__version__}: {document.headline}")
    typer.echo(f"Mode: {document.mode}" + (f" ({document.mode_reason})" if document.mode_reason else ""))
    typer.echo(
        f"{len(document.stories)} stories from {document.candidate_count} candidates · "
        f"{total} sources checked"
    )
    if unavailable:
        typer.echo(f"{unavailable} source{'s' if unavailable != 1 else ''} unavailable")
    if not document.stories:
        typer.echo("Quiet feed window: no recent stories matched these interests.")
    try:
        local_zone = ZoneInfo(interests.settings.local_timezone)
    except ZoneInfoNotFoundError:
        local_zone = datetime.now().astimezone().tzinfo or UTC
    for bullet in document.tldr:
        typer.echo(f"• {bullet}")
    for index, story in enumerate(document.stories, start=1):
        when = story.published_at.astimezone(local_zone).strftime("%H:%M %Z")
        marker = " [ARCH MANUAL ACTION]" if story.manual_priority else ""
        outlets = f" · {story.outlet_count} outlets" if story.outlet_count > 1 else ""
        typer.echo(f"{index:02d}. [{story.category}] {story.title}{marker}")
        typer.echo(
            f"    importance {story.importance}/10 · {', '.join(story.source_names)} · "
            f"{when}{outlets}"
        )
        typer.echo(f"    {story.link}")
        typer.echo(f"    {story.summary}")
        typer.echo(f"    Why it matters: {story.why_it_matters}")
    if not dry_run:
        typer.echo(f"Saved: {paths.state_dir / f'briefing-{document.date}.json'}")


@app.command()
def trigger(
    config: Optional[Path] = typer.Option(None, "--config", help="Interests TOML (defaults to XDG config)."),
) -> None:
    """Run once in the configured morning window when session, timer or resume starts it."""
    paths = Paths.current()
    config_path = (config or paths.interests_file).expanduser()
    try:
        interests = load_config(config_path)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(paths.state_dir, 0o700)
    except OSError:
        pass
    status = StatusWriter(paths.status_file)
    now = datetime.now(UTC)
    trigger_timezone = interests.settings.local_timezone
    try:
        local_zone = ZoneInfo(interests.settings.local_timezone)
    except ZoneInfoNotFoundError:
        local_zone = UTC
        trigger_timezone = "UTC"
    local_day = now.astimezone(local_zone).date().isoformat()

    try:
        lock = TriggerLock(paths.trigger_lock)
        lock.__enter__()
    except BlockingIOError:
        typer.echo("Morning Bar trigger is already running.")
        raise typer.Exit(code=0)
    try:
        state = TriggerState(paths.trigger_state_file)
        if not is_in_morning_window(
            now,
            trigger_timezone,
            interests.settings.morning_start,
            interests.settings.morning_end,
            interests.settings.late_grace_minutes,
        ):
            last_outside = state.last_outside_event(local_day)
            if last_outside is not None:
                elapsed = (now - last_outside).total_seconds()
                if 0 <= elapsed < interests.settings.trigger_debounce_seconds:
                    _record_trigger_skip(status, paths, local_day, "outside-window-debounced")
                    typer.echo("Outside-window duplicate trigger was debounced.")
                    return
            state.mark_outside_event(local_day, now)
            _record_trigger_skip(status, paths, local_day, "outside-morning-window")
            typer.echo("Outside the configured morning window; no run started.")
            return
        if state.completed_today(local_day):
            _record_trigger_skip(status, paths, local_day, "already-completed-today")
            typer.echo("Morning Bar already completed today.")
            return
        last_started = state.last_started()
        if last_started is not None:
            elapsed = (now - last_started).total_seconds()
            if 0 <= elapsed < interests.settings.trigger_debounce_seconds:
                _record_trigger_skip(status, paths, local_day, "trigger-debounced")
                typer.echo("Duplicate trigger was debounced.")
                return

        state.mark_started(now)
        status.update("trigger-starting", 0, len(interests.sources), local_day=local_day)
        ui_started = False
        if not _ui_dismissed(paths, local_day):
            ui_started = _start_ui_service()
            if not ui_started:
                logging.getLogger(__name__).warning("Morning Bar UI could not be started; will use the desktop fallback")
        network = wait_for_network(interests.settings, status)
        offline = not network.connected
        if offline:
            logging.getLogger(__name__).warning("network unavailable; using cached articles only")
        if network.metered:
            logging.getLogger(__name__).info("NetworkManager reports a metered connection; using reduced fetch limits")
            settings = interests.settings.model_copy(
                update={
                    "max_candidates": min(interests.settings.max_candidates, 100),
                    "max_feed_items": min(interests.settings.max_feed_items, 25),
                    "max_body_bytes": min(interests.settings.max_body_bytes, 1_000_000),
                }
            )
            interests = interests.model_copy(update={"settings": settings})

        store = Store(paths.database_file)
        try:
            document, _, unavailable, total = asyncio.run(
                _run_pipeline(
                    interests,
                    store,
                    status,
                    paths,
                    no_llm=offline,
                    dry_run=False,
                    offline=offline,
                )
            )
        except KeyboardInterrupt:
            status.update("interrupted", local_day=local_day)
            raise typer.Exit(code=130)
        state.mark_completed(local_day, datetime.now(UTC))
        status.update(
            "ready", len(document.stories), len(document.stories),
            local_day=local_day, offline=offline, metered=network.metered,
            unavailable=unavailable, sources_checked=total,
            mode=document.mode, mode_reason=document.mode_reason,
            briefing_date=document.date,
        )
        if not ui_started and not _ui_dismissed(paths, local_day):
            _fallback_ui(paths, open_page=False)
        typer.echo(
            f"Morning Bar trigger finished: {len(document.stories)} stories; "
            f"mode={document.mode}; offline={str(offline).lower()}; "
            f"metered={str(network.metered).lower()}"
        )
    except typer.Exit:
        raise
    except Exception as exc:
        status.update("failed", local_day=local_day, failure_type=type(exc).__name__)
        logging.getLogger(__name__).warning("Morning Bar trigger failed (%s)", type(exc).__name__)
        if not _ui_dismissed(paths, local_day):
            _fallback_ui(paths, open_page=False)
        raise
    finally:
        lock.__exit__(None, None, None)


@app.command("status")
def status_command() -> None:
    """Show the latest collector status."""
    path = Paths.current().status_file
    if not path.exists():
        typer.echo("No run recorded yet.")
        return
    try:
        typer.echo(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"Could not read status: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("config-path")
def config_path() -> None:
    """Print the active XDG config and state paths."""
    paths = Paths.current()
    typer.echo(f"config: {paths.interests_file}")
    typer.echo(f"state:  {paths.state_dir}")


@app.command()
def show(
    expand: bool = typer.Option(False, "--expand", help="Open the briefing card and focus it."),
) -> None:
    """Summon the Morning Bar UI, including after dismissing it today."""
    paths = Paths.current()
    try:
        interests = load_config(paths.interests_file)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_day = _local_day(interests.settings.local_timezone)
    _write_json(paths.ui_state_file, {"dismissed_local_day": None, "updated_at": datetime.now(UTC).isoformat()})
    _write_json(paths.ui_request_file, {
        "action": "show", "expand": expand, "date": local_day,
        "request_id": datetime.now(UTC).isoformat(),
    })
    if _start_ui_service():
        typer.echo("Morning Bar summoned." + (" Expanded and focused." if expand else ""))
        return
    _fallback_ui(paths, open_page=True)
    typer.echo("Quickshell could not start; showed a notification and opened the local HTML briefing.", err=True)


@app.command(hidden=True)
def ui() -> None:
    """Internal systemd entrypoint that exports XDG paths before starting Quickshell."""
    paths = Paths.current()
    try:
        interests = load_config(paths.interests_file)
    except ValueError as exc:
        logging.getLogger(__name__).error("Cannot start Morning Bar UI: invalid interests config")
        raise typer.Exit(code=2) from exc
    local_day = _local_day(interests.settings.local_timezone)
    latest = paths.state_dir / f"briefing-{local_day}.json"
    os.environ.update({
        "MORNINGBAR_CLI": os.path.abspath(sys.argv[0]),
        "MORNINGBAR_LOCAL_DATE": local_day,
        "MORNINGBAR_STATUS_FILE": str(paths.status_file),
        "MORNINGBAR_BRIEFING_FILE": str(latest),
        "MORNINGBAR_UI_STATE_FILE": str(paths.ui_state_file),
        "MORNINGBAR_UI_REQUEST_FILE": str(paths.ui_request_file),
    })
    qs = shutil.which("qs")
    if not qs:
        typer.echo("Quickshell executable `qs` is not installed.", err=True)
        raise typer.Exit(code=1)
    os.execv(qs, [qs, "-c", "morningbar"])


@app.command(hidden=True)
def dismiss() -> None:
    """Record that the UI was dismissed for this local day."""
    paths = Paths.current()
    try:
        interests = load_config(paths.interests_file)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_json(paths.ui_state_file, {
        "dismissed_local_day": _local_day(interests.settings.local_timezone),
        "updated_at": datetime.now(UTC).isoformat(),
    })


@app.command()
def save(story_id: str = typer.Argument(..., help="Story id from a saved briefing.")) -> None:
    """Save a briefing story locally for later."""
    paths = Paths.current()
    story = _find_story(paths.state_dir, story_id)
    if story is None:
        typer.echo(f"Story {story_id} was not found in a saved briefing.", err=True)
        raise typer.Exit(code=2)
    Store(paths.database_file).save_for_later(
        story_id, story["title"], story["link"], story["category_id"], story["source_names"],
    )
    typer.echo("Story saved locally.")


@app.command()
def feedback(
    story_id: str = typer.Argument(..., help="Story id from the latest briefing JSON."),
    vote: Optional[Literal["up", "down"]] = typer.Option(None, "--vote", help="Rate this story."),
    less_like: bool = typer.Option(False, "--less-like", help="Reduce similar topic stories."),
) -> None:
    """Save local feedback to gently tune topic ranking."""
    if vote is None and not less_like:
        typer.echo("Choose --vote up/down or --less-like.", err=True)
        raise typer.Exit(code=2)
    paths = Paths.current()
    topic_id = _find_story_topic(paths.state_dir, story_id)
    if topic_id is None:
        typer.echo(f"Story {story_id} was not found in a saved briefing.", err=True)
        raise typer.Exit(code=2)
    vote_value = 1 if vote == "up" else -1 if vote == "down" else 0
    Store(paths.database_file).save_feedback(
        story_id,
        topic_id,
        vote=vote_value if vote is not None else None,
        less_like=less_like if less_like else None,
    )
    typer.echo("Feedback saved locally.")


async def _run_pipeline(
    interests,
    store: Store,
    status: StatusWriter,
    paths: Paths,
    *,
    no_llm: bool,
    dry_run: bool,
    offline: bool = False,
) -> tuple[BriefingDocument, list[Story], int, int]:
    stories, unavailable, total = await Collector(interests, store, status).collect(offline=offline)
    mode_reason: str | None = None
    result = GeminiResult({}, None, None, "local preview")
    if not dry_run and not no_llm:
        try:
            result = await GeminiRanker(
                interests.gemini,
                paths.gemini_key_file,
                interests.topics,
                store,
                status,
            ).create_briefing(stories)
        except Exception as exc:
            # Do not expose exception text; provider errors may contain request data.
            logging.getLogger(__name__).warning("Gemini disabled for this run (%s)", type(exc).__name__)
            result = GeminiResult({}, None, None, "Gemini unavailable")
    elif dry_run:
        mode_reason = "dry-run uses local ranking"
    elif no_llm:
        mode_reason = "offline; cached items only" if offline else "--no-llm requested"

    gemini_ok = bool(result.digest is not None and len(result.rankings) == len(stories))
    mode = "gemini" if gemini_ok else "basic"
    if gemini_ok:
        mode_reason = None
    elif mode_reason is None:
        mode_reason = result.reason or "local ranking fallback"

    topics = {topic.id: topic for topic in interests.topics}
    briefing_stories: list[BriefingStory] = []
    for story in stories:
        rank = result.rankings.get(story.id) if gemini_ok else None
        if rank is None:
            briefing_stories.append(local_briefing_story(story, topics[story.topic_id]))
            continue
        category = topics[rank.category_id]
        briefing_stories.append(
            BriefingStory(
                id=story.id,
                category_id=category.id,
                category=category.label,
                title=story.title,
                link=story.link,
                source_names=story.source_names,
                published_at=story.published_at,
                excerpt=story.excerpt[:600],
                summary=rank.summary.strip(),
                why_it_matters=rank.why_it_matters.strip(),
                importance=rank.importance,
                local_score=story.score,
                outlet_count=story.outlet_count,
                manual_priority=story.manual_priority,
            )
        )

    selected = select_final(
        briefing_stories,
        max_stories=interests.gemini.max_stories,
        max_per_category=interests.gemini.max_per_category,
    )
    if gemini_ok:
        headline = result.digest.headline.strip()
        tldr = [item.strip() for item in result.digest.tldr if item.strip()]
        model = result.model
    else:
        headline, tldr = basic_digest(selected)
        model = None

    try:
        local_zone = ZoneInfo(interests.settings.local_timezone)
    except ZoneInfoNotFoundError:
        local_zone = datetime.now().astimezone().tzinfo or UTC
    day = datetime.now(local_zone).date().isoformat()
    document = make_document(
        day=day,
        stories=selected,
        mode=mode,
        mode_reason=mode_reason,
        model=model,
        headline=headline,
        tldr=tldr,
        candidate_count=len(stories),
        source_count=total,
        unavailable_sources=unavailable,
    )
    if not dry_run:
        _write_briefing(paths.state_dir / f"briefing-{day}.json", document)
        store.mark_shown([story.id for story in selected])
    status.update(
        "ready", len(selected), len(selected), unavailable=unavailable,
        candidates=len(stories), stories=len(selected), mode=mode,
        mode_reason=mode_reason, dry_run=dry_run,
        briefing_date=day, finished_at=datetime.now(UTC).isoformat(),
        gemini_usage=store.gemini_usage_today(),
    )
    return document, stories, unavailable, total


def _write_briefing(path: Path, document: BriefingDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix="briefing-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _record_trigger_skip(status: StatusWriter, paths: Paths, local_day: str, reason: str) -> None:
    """Keep a current day's briefing discoverable after a later gated event."""
    path = paths.state_dir / f"briefing-{local_day}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stories = payload.get("stories", [])
        if payload.get("date") != local_day or not isinstance(stories, list):
            raise ValueError("briefing does not match today")
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        status.update(reason, local_day=local_day)
        return
    count = len(stories)
    status.update(
        "ready", count, count, local_day=local_day, briefing_date=local_day,
        mode=payload.get("mode", "basic"), trigger_gate=reason,
    )


def _find_story_topic(state_dir: Path, story_id: str) -> str | None:
    story = _find_story(state_dir, story_id)
    return str(story["category_id"]) if story is not None else None


def _find_story(state_dir: Path, story_id: str) -> dict | None:
    for path in sorted(state_dir.glob("briefing-*.json"), reverse=True):
        try:
            briefing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for story in briefing.get("stories", []):
            if story.get("id") == story_id:
                return story
    return None


def _local_day(timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = datetime.now().astimezone().tzinfo or UTC
    return datetime.now(UTC).astimezone(zone).date().isoformat()


def _ui_dismissed(paths: Paths, local_day: str) -> bool:
    try:
        state = json.loads(paths.ui_state_file.read_text(encoding="utf-8"))
        return state.get("dismissed_local_day") == local_day
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _start_ui_service() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "morningbar-ui.service"],
            check=False, capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _fallback_ui(paths: Paths, *, open_page: bool) -> None:
    briefings = sorted(paths.state_dir.glob("briefing-*.json"), reverse=True)
    document: dict = {}
    if briefings:
        try:
            document = json.loads(briefings[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    stories = document.get("stories", [])[:3]
    title = "Morning Bar · basic mode"
    body = "\n".join(
        f"{item.get('title', 'Untitled')} — {item.get('summary', '')[:160]}"
        for item in stories
    ) or "Quickshell could not start and no saved briefing is available yet."
    notify = shutil.which("notify-send")
    if notify:
        try:
            subprocess.run([notify, "-u", "low", title, body], check=False, timeout=3, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    cards = "".join(
        "<article><p class='meta'>" + html.escape(" · ".join(item.get("source_names", []))) +
        "</p><h2><a href='" + html.escape(item.get("link", "#"), quote=True) + "'>" +
        html.escape(item.get("title", "Untitled")) + "</a></h2><p>" +
        html.escape(item.get("summary", "")) + "</p><p class='why'>" +
        html.escape(item.get("why_it_matters", "")) + "</p></article>"
        for item in stories
    ) or "<p>No saved briefing is available yet.</p>"
    page = paths.state_dir / "briefing.html"
    page.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    page.write_text(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Morning Bar</title><style>body{margin:3rem auto;max-width:46rem;padding:0 1.5rem;"
        "background:#111820;color:#e8edf5;font:16px/1.6 system-ui}h1{color:#b9c9ff}"
        "article{margin:1.2rem 0;padding:1.3rem;border:1px solid #303a46;border-radius:18px;"
        "background:#1a232d}a{color:#c7d4ff}.meta,.why{color:#9aa8b8}</style>"
        f"<h1>{html.escape(document.get('headline', 'Morning Bar'))}</h1><p>{html.escape(document.get('mode', 'basic'))} mode</p>{cards}",
        encoding="utf-8",
    )
    try:
        page.chmod(0o600)
    except OSError:
        pass
    if open_page:
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    app()

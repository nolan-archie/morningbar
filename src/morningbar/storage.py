from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Article


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS feed_cache (
                    source_id TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_host TEXT NOT NULL,
                    trust INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    excerpt TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS articles_by_source ON articles(source_id, published_at);
                CREATE TABLE IF NOT EXISTS shown (
                    story_id TEXT PRIMARY KEY,
                    shown_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS page_snapshots (
                    source_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    story_id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    vote INTEGER NOT NULL DEFAULT 0 CHECK (vote IN (-1, 0, 1)),
                    less_like INTEGER NOT NULL DEFAULT 0 CHECK (less_like IN (0, 1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS saved_stories (
                    story_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    source_names TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gemini_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usage_date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    requested_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS gemini_calls_by_day ON gemini_calls(usage_date);
                CREATE INDEX IF NOT EXISTS gemini_calls_by_time ON gemini_calls(requested_at);
                """
            )
            db.execute(
                "DELETE FROM gemini_calls WHERE requested_at < ?",
                ((datetime.now(UTC) - timedelta(days=180)).isoformat(),),
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def request_headers(self, source_id: str) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT etag, last_modified FROM feed_cache WHERE source_id = ?", (source_id,)
            ).fetchone()
        if not row:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def set_response_cache(
        self, source_id: str, etag: str | None, last_modified: str | None
    ) -> None:
        fetched_at = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO feed_cache(source_id, etag, last_modified, fetched_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       etag = excluded.etag,
                       last_modified = excluded.last_modified,
                       fetched_at = excluded.fetched_at""",
                (source_id, etag, last_modified, fetched_at),
            )

    def save_articles(self, articles: list[Article]) -> None:
        if not articles:
            return
        with self.connect() as db:
            db.executemany(
                """INSERT INTO articles
                   (id, topic_id, source_id, source_name, source_host, trust,
                    title, link, published_at, excerpt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title, link=excluded.link,
                     published_at=excluded.published_at, excerpt=excluded.excerpt""",
                [
                    (
                        item.id,
                        item.topic_id,
                        item.source_id,
                        item.source_name,
                        item.source_host,
                        item.trust,
                        item.title,
                        item.link,
                        item.published_at.astimezone(UTC).isoformat(),
                        item.excerpt,
                    )
                    for item in articles
                ],
            )

    def cached_articles(self, source_id: str, since: datetime) -> list[Article]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM articles WHERE source_id = ? AND published_at >= ?",
                (source_id, since.astimezone(UTC).isoformat()),
            ).fetchall()
        return [
            Article(
                id=row["id"],
                topic_id=row["topic_id"],
                source_id=row["source_id"],
                source_name=row["source_name"],
                source_host=row["source_host"],
                trust=row["trust"],
                title=row["title"],
                link=row["link"],
                published_at=datetime.fromisoformat(row["published_at"]),
                excerpt=row["excerpt"],
            )
            for row in rows
        ]

    def shown_ids(self, since: datetime) -> set[str]:
        with self.connect() as db:
            rows = db.execute("SELECT story_id FROM shown WHERE shown_at >= ?", (since.isoformat(),))
            return {row[0] for row in rows}

    def mark_shown(self, story_ids: list[str], when: datetime | None = None) -> None:
        if not story_ids:
            return
        stamp = (when or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO shown(story_id, shown_at) VALUES (?, ?) "
                "ON CONFLICT(story_id) DO UPDATE SET shown_at=excluded.shown_at",
                [(story_id, stamp) for story_id in story_ids],
            )

    def prune(self, articles_before: datetime, shown_before: datetime) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM articles WHERE published_at < ?", (articles_before.isoformat(),))
            db.execute("DELETE FROM shown WHERE shown_at < ?", (shown_before.isoformat(),))

    def page_snapshot(self, source_id: str) -> tuple[str, str] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT content_hash, excerpt FROM page_snapshots WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return (row["content_hash"], row["excerpt"]) if row else None

    def set_page_snapshot(self, source_id: str, content_hash: str, excerpt: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO page_snapshots(source_id, content_hash, excerpt, checked_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     excerpt=excluded.excerpt,
                     checked_at=excluded.checked_at""",
                (source_id, content_hash, excerpt[:1500], datetime.now(UTC).isoformat()),
            )

    def save_feedback(
        self, story_id: str, topic_id: str, *, vote: int | None = None, less_like: bool | None = None
    ) -> None:
        if vote is not None and vote not in {-1, 0, 1}:
            raise ValueError("vote must be -1, 0, or 1")
        with self.connect() as db:
            previous = db.execute(
                "SELECT vote, less_like FROM feedback WHERE story_id = ?", (story_id,)
            ).fetchone()
            merged_vote = vote if vote is not None else (previous["vote"] if previous else 0)
            merged_less_like = (
                less_like if less_like is not None else bool(previous["less_like"] if previous else False)
            )
            db.execute(
                """INSERT INTO feedback(story_id, topic_id, vote, less_like, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(story_id) DO UPDATE SET topic_id=excluded.topic_id,
                     vote=excluded.vote, less_like=excluded.less_like,
                     updated_at=excluded.updated_at""",
                (story_id, topic_id, merged_vote, int(merged_less_like), datetime.now(UTC).isoformat()),
            )

    def save_for_later(
        self,
        story_id: str,
        title: str,
        link: str,
        category_id: str,
        source_names: list[str],
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO saved_stories
                   (story_id, title, link, category_id, source_names, saved_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(story_id) DO UPDATE SET
                     title=excluded.title, link=excluded.link,
                     category_id=excluded.category_id,
                     source_names=excluded.source_names,
                     saved_at=excluded.saved_at""",
                (
                    story_id, title, link, category_id,
                    json.dumps(source_names, ensure_ascii=False), datetime.now(UTC).isoformat(),
                ),
            )

    def topic_score_adjustments(self) -> dict[str, float]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT topic_id, SUM(vote * 0.25 - less_like * 0.5) AS adjustment
                   FROM feedback GROUP BY topic_id"""
            ).fetchall()
        return {
            row["topic_id"]: max(-2.0, min(2.0, float(row["adjustment"] or 0.0)))
            for row in rows
        }

    @staticmethod
    def gemini_usage_date(now: datetime | None = None) -> str:
        """Gemini's documented daily quota boundary is midnight Pacific time."""
        return (now or datetime.now(UTC)).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def reserve_gemini_call(
        self,
        model: str,
        estimated_input_tokens: int,
        reserved_output_tokens: int,
        *,
        max_requests_per_minute: int,
        max_requests_per_day: int,
        max_tokens_per_minute: int,
        max_tokens_per_day: int,
        now: datetime | None = None,
    ) -> tuple[int | None, str | None]:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        stamp_text = stamp.isoformat()
        usage_date = self.gemini_usage_date(stamp)
        reserved_tokens = estimated_input_tokens + reserved_output_tokens
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            daily = db.execute(
                """SELECT COUNT(*) AS requests,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                   FROM gemini_calls WHERE usage_date = ?""",
                (usage_date,),
            ).fetchone()
            recent = db.execute(
                """SELECT COUNT(*) AS requests,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                   FROM gemini_calls WHERE requested_at >= ?""",
                ((stamp - timedelta(seconds=60)).isoformat(),),
            ).fetchone()
            if daily["requests"] >= max_requests_per_day:
                return None, "daily request cap reached"
            if daily["tokens"] + reserved_tokens > max_tokens_per_day:
                return None, "daily token cap near"
            if recent["requests"] >= max_requests_per_minute:
                return None, "request rate cap reached"
            if recent["tokens"] + reserved_tokens > max_tokens_per_minute:
                return None, "minute token cap reached"
            cursor = db.execute(
                """INSERT INTO gemini_calls
                   (usage_date, model, input_tokens, output_tokens, requested_at, succeeded)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (usage_date, model, estimated_input_tokens, reserved_output_tokens, stamp_text),
            )
            return int(cursor.lastrowid), None

    def finish_gemini_call(
        self, call_id: int, actual_input_tokens: int | None, actual_output_tokens: int | None
    ) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT input_tokens, output_tokens FROM gemini_calls WHERE id = ?", (call_id,)
            ).fetchone()
            if row is None:
                return
            input_tokens = max(0, actual_input_tokens) if actual_input_tokens is not None else row["input_tokens"]
            output_tokens = max(0, actual_output_tokens) if actual_output_tokens is not None else row["output_tokens"]
            db.execute(
                """UPDATE gemini_calls SET input_tokens = ?, output_tokens = ?, succeeded = 1
                   WHERE id = ?""",
                (input_tokens, output_tokens, call_id),
            )

    def gemini_usage_today(self, now: datetime | None = None) -> dict[str, int | str]:
        usage_date = self.gemini_usage_date(now)
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS requests,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                   FROM gemini_calls WHERE usage_date = ?""",
                (usage_date,),
            ).fetchone()
        return {"date": usage_date, "requests": int(row["requests"]), "tokens": int(row["tokens"])}

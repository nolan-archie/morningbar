from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_hours: int = Field(default=36, ge=1, le=168)
    shown_cooldown_hours: int = Field(default=72, ge=1, le=720)
    max_candidates: int = Field(default=150, ge=1, le=500)
    max_feed_items: int = Field(default=50, ge=1, le=200)
    max_body_bytes: int = Field(default=4_000_000, ge=100_000, le=100_000_000)
    request_interval_seconds: float = Field(default=1.0, ge=1.0, le=60.0)
    local_timezone: str = "Africa/Johannesburg"
    morning_start: time = time(5, 0)
    morning_end: time = time(11, 0)
    late_grace_minutes: int = Field(default=0, ge=0, le=360)
    trigger_debounce_seconds: int = Field(default=60, ge=0, le=3600)
    network_wait_seconds: int = Field(default=240, ge=0, le=900)

    @model_validator(mode="after")
    def valid_morning_window(self) -> "Settings":
        if self.morning_start >= self.morning_end:
            raise ValueError("morning_start must be earlier than morning_end")
        return self


class GeminiSettings(BaseModel):
    """App-side safety caps; Google's live project limits remain authoritative."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    primary_model: str = "gemini-3.5-flash-lite"
    fallback_model: str = "gemini-3.8-flash"
    batch_size: int = Field(default=20, ge=1, le=50)
    max_stories: int = Field(default=15, ge=1, le=15)
    max_per_category: int = Field(default=3, ge=1, le=15)
    max_requests_per_minute: int = Field(default=8, ge=1, le=60)
    max_requests_per_run: int = Field(default=8, ge=1, le=20)
    max_requests_per_day: int = Field(default=12, ge=1, le=1000)
    max_tokens_per_minute: int = Field(default=40_000, ge=1_000, le=1_000_000)
    max_tokens_per_day: int = Field(default=50_000, ge=1_000, le=10_000_000)
    max_output_tokens: int = Field(default=2_400, ge=256, le=16_384)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1.0, ge=0.25, le=30.0)
    minimum_request_interval_seconds: float = Field(default=4.0, ge=1.0, le=60.0)


class TopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str
    weight: int = Field(ge=1, le=5)
    keywords: list[str] = Field(default_factory=list)
    keyword_boosts: dict[str, float] = Field(default_factory=dict)
    manual_priority: bool = False
    news_only: bool = False
    watchlist: list[str] = Field(default_factory=list)

    @field_validator("keyword_boosts")
    @classmethod
    def valid_boosts(cls, value: dict[str, float]) -> dict[str, float]:
        if any(boost < 0 or boost > 10 for boost in value.values()):
            raise ValueError("keyword boosts must be between 0 and 10")
        return value


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    topic: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    url: str
    kind: Literal["feed", "hackernews", "huggingface", "anilist", "ical", "page"] = "feed"
    trust: int = Field(default=3, ge=1, le=5)
    max_items: int = Field(default=50, ge=1, le=200)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        parsed = HttpUrl(value)
        return str(parsed)


class InterestsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: Settings = Field(default_factory=Settings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    topics: list[TopicConfig]
    sources: list[SourceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_references(self) -> "InterestsConfig":
        topic_ids = [topic.id for topic in self.topics]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("topic ids must be unique")
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        missing = {source.topic for source in self.sources} - set(topic_ids)
        if missing:
            raise ValueError(f"sources reference unknown topics: {', '.join(sorted(missing))}")
        return self


class Article(BaseModel):
    id: str
    topic_id: str
    source_id: str
    source_name: str
    source_host: str
    trust: int
    title: str
    link: str
    published_at: datetime
    excerpt: str = ""


class Story(BaseModel):
    id: str
    topic_id: str
    topic_label: str
    title: str
    link: str
    source_names: list[str]
    published_at: datetime
    excerpt: str
    score: float
    outlet_count: int
    manual_priority: bool = False

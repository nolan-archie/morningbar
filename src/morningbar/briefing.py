from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Story, TopicConfig


class BriefingStory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category_id: str
    category: str
    title: str
    link: str
    source_names: list[str]
    published_at: datetime
    excerpt: str = ""
    summary: str
    why_it_matters: str
    importance: int = Field(ge=1, le=10)
    local_score: float
    outlet_count: int
    manual_priority: bool = False


class BriefingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    generated_at: datetime
    mode: Literal["gemini", "basic"]
    mode_reason: str | None = None
    model: str | None = None
    headline: str
    tldr: list[str] = Field(default_factory=list, max_length=3)
    stories: list[BriefingStory]
    candidate_count: int
    source_count: int
    unavailable_sources: int


class StoryRank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    importance: int = Field(ge=1, le=10)
    category_id: str
    summary: str = Field(min_length=1, max_length=500)
    why_it_matters: str = Field(min_length=1, max_length=220)


class RankingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StoryRank]


class DigestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=180)
    tldr: list[str] = Field(max_length=3)


def select_final(
    stories: list[BriefingStory], *, max_stories: int, max_per_category: int
) -> list[BriefingStory]:
    ordered = sorted(stories, key=lambda item: (item.manual_priority, item.importance, item.local_score), reverse=True)
    chosen: list[BriefingStory] = []
    counts: dict[str, int] = {}

    # Arch manual actions always bypass category caps and remain at the top.
    for item in ordered:
        if item.manual_priority and len(chosen) < max_stories:
            chosen.append(item)
            counts[item.category_id] = counts.get(item.category_id, 0) + 1

    for item in ordered:
        if len(chosen) >= max_stories:
            break
        if item.manual_priority:
            continue
        if counts.get(item.category_id, 0) >= max_per_category:
            continue
        chosen.append(item)
        counts[item.category_id] = counts.get(item.category_id, 0) + 1
    return chosen


def local_briefing_story(story: Story, topic: TopicConfig) -> BriefingStory:
    excerpt = story.excerpt.strip()
    summary = excerpt[:500] if excerpt else "Open the source for details."
    matched = [
        keyword for keyword in topic.keywords
        if keyword.casefold() in f"{story.title} {excerpt}".casefold()
    ]
    if matched:
        why = f"Matches your interest in {matched[0]}."
    else:
        why = f"Matches your {topic.label.lower()} interests."
    importance = 10 if story.manual_priority else min(10, max(1, round(story.score * 0.4)))
    return BriefingStory(
        id=story.id,
        category_id=topic.id,
        category=topic.label,
        title=story.title,
        link=story.link,
        source_names=story.source_names,
        published_at=story.published_at,
        excerpt=excerpt[:600],
        summary=summary,
        why_it_matters=why,
        importance=importance,
        local_score=story.score,
        outlet_count=story.outlet_count,
        manual_priority=story.manual_priority,
    )


def basic_digest(stories: list[BriefingStory]) -> tuple[str, list[str]]:
    if not stories:
        return "A quiet morning across your interests.", []
    categories = len({item.category_id for item in stories})
    headline = f"Good morning. {len(stories)} updates across {categories} areas."
    bullets = [
        f"{item.title} — {item.summary[:180]}".rstrip(" —")
        for item in stories[:3]
    ]
    return headline, bullets


def make_document(
    *,
    day: str,
    stories: list[BriefingStory],
    mode: Literal["gemini", "basic"],
    mode_reason: str | None,
    model: str | None,
    headline: str,
    tldr: list[str],
    candidate_count: int,
    source_count: int,
    unavailable_sources: int,
) -> BriefingDocument:
    return BriefingDocument(
        date=day,
        generated_at=datetime.now(UTC),
        mode=mode,
        mode_reason=mode_reason,
        model=model,
        headline=headline,
        tldr=tldr[:3],
        stories=stories,
        candidate_count=candidate_count,
        source_count=source_count,
        unavailable_sources=unavailable_sources,
    )

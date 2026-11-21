from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from .models import InterestsConfig


def load_config(path: Path) -> InterestsConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return InterestsConfig.model_validate(raw)
    except FileNotFoundError as exc:
        raise ValueError(f"interests config not found: {path}") from exc
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid interests config {path}: {exc}") from exc

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StatusWriter:
    def __init__(self, path: Path):
        self.path = path

    def update(self, stage: str, progress: int = 0, total: int = 0, **extra: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stage": stage,
            "progress": progress,
            "total": total,
            "updated_at": datetime.now(UTC).isoformat(),
            **extra,
        }
        fd, tmp_name = tempfile.mkstemp(prefix="status-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

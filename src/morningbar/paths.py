from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xdg_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else fallback


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path

    @classmethod
    def current(cls) -> "Paths":
        home = Path.home()
        config_root = _xdg_path("XDG_CONFIG_HOME", home / ".config")
        state_root = _xdg_path("XDG_STATE_HOME", home / ".local" / "state")
        return cls(config_root / "morningbar", state_root / "morningbar")

    @property
    def interests_file(self) -> Path:
        return self.config_dir / "interests.toml"

    @property
    def gemini_key_file(self) -> Path:
        return self.config_dir / "gemini-api-key"

    @property
    def database_file(self) -> Path:
        return self.state_dir / "morningbar.sqlite3"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def trigger_lock(self) -> Path:
        return self.state_dir / "trigger.lock"

    @property
    def trigger_state_file(self) -> Path:
        return self.state_dir / "trigger-state.json"

    @property
    def ui_state_file(self) -> Path:
        return self.state_dir / "ui-state.json"

    @property
    def ui_request_file(self) -> Path:
        return self.state_dir / "ui-request.json"

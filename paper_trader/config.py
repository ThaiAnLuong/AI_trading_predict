"""File locations and local secrets.

API keys are never written in code. They come from real environment variables or from
"source API/api_keys.env", which stays on this computer and is excluded from git.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = PROJECT_ROOT / "source API" / "api_keys.env"


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    demo: bool

    @property
    def db(self) -> Path:
        # Demo data lives in a separate file so it can never mix with the experiment.
        return self.data_dir / ("demo_FAKE_DATA.sqlite3" if self.demo else "paper_trading.sqlite3")

    @property
    def lessons(self) -> Path:
        return self.data_dir / ("ai_lessons_DEMO.txt" if self.demo else "ai_lessons.txt")

    @property
    def backups(self) -> Path:
        return self.data_dir / ("backups_demo" if self.demo else "backups")

    @property
    def exports(self) -> Path:
        return self.data_dir / ("exports_demo" if self.demo else "exports")


def get_paths(demo: bool, data_dir: str | os.PathLike | None = None) -> Paths:
    base = Path(data_dir or os.environ.get("PAPER_TRADER_DATA_DIR") or PROJECT_ROOT / "data")
    paths = Paths(base, demo)
    for folder in (paths.data_dir, paths.backups, paths.exports):
        folder.mkdir(parents=True, exist_ok=True)
    return paths


def load_api_keys(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the keys file, without overriding real environment variables."""
    path = path or KEYS_FILE
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def api_key(name: str) -> str | None:
    return (os.environ.get(name) or "").strip() or None

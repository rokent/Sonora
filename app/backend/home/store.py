"""Where Sonora Home keeps its files, and how it writes them.

One folder under the data dir, so a backup or a wipe is one path. Writes go
through a temporary file and a rename, so a crash never leaves half a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..config import settings


def home_dir() -> Path:
    """``<data dir>/home``, made if missing. The data dir is read on each
    call, not at import, so it can be pointed elsewhere (tests do)."""
    path = Path(settings.data_dir) / "home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any) -> Any:
    """The JSON in ``path``, or ``default`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)

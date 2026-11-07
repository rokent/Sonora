"""Where Sonora Home keeps its files, and how it writes them.

One folder under the data dir, so a backup or a wipe is one path. Writes go
through a temporary file and a rename, so a crash never leaves half a file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)


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


def load_json(path: Path) -> Any:
    """The JSON in ``path``, or None when it is missing or unreadable. A file
    that is there but is not JSON is set aside as ``<name>.corrupt-<unix
    time>`` (and a warning logged), so starting empty never overwrites what
    someone may still want to rescue."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("home: %s could not be read: %s", path, exc)
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, aside)
            log.warning("home: %s is not valid JSON (%s); moved to %s, starting empty", path, exc, aside.name)
        except OSError as move_exc:
            log.warning("home: %s is not valid JSON (%s) and could not be moved aside: %s", path, exc, move_exc)
        return None


def write_json(path: Path, data: Any) -> None:
    """Replace ``path`` with ``data``: a private temp file in the same folder,
    flushed to disk, then renamed over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp creates it 0600
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

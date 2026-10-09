"""Skip-list record so batch runs can resume without reprocessing.

Each entry stores the output path, a signature (size + mtime) of the source,
and the processing options used. If the source changed OR the user changed
model / scale / face-enhance / interpolation settings, the file is redone.
"""

import json
import threading
from pathlib import Path

from .config import load

_lock = threading.Lock()


def record_path() -> Path:
    root = Path(load()["output_root"])
    root.mkdir(parents=True, exist_ok=True)
    return root / ".processed.json"


def _sig(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}-{int(st.st_mtime_ns)}"


def _load() -> dict:
    p = record_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def done_info(src: Path, opts: dict | None = None):
    """Return the output Path for an already-processed source, or None.

    opts must match what was stored for the entry to count as done.
    """
    rec = _load()
    ent = rec.get(str(src.resolve()))
    if not ent:
        return None
    try:
        out = Path(ent["out"])
        if out.exists() and ent.get("sig") == _sig(src):
            if opts is None or ent.get("opts") == opts:
                return out
    except Exception:
        return None
    return None


def mark_done(src: Path, out: Path, opts: dict | None = None) -> None:
    with _lock:
        rec = _load()
        rec[str(src.resolve())] = {
            "out": str(out),
            "sig": _sig(src),
            "opts": opts or {},
        }
        record_path().write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8"
        )
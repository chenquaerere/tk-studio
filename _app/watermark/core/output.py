"""Output path helpers: date-based save directories + conflict-free names."""

import datetime
import re
import unicodedata
from pathlib import Path

from .config import load


def sanitize(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip().strip(".")
    return name or "file"


def output_root() -> Path:
    root = Path(load()["output_root"]).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def day_dir(when: datetime.date | None = None) -> Path:
    d = datetime.date.today() if when is None else when
    return output_root() / d.strftime("%Y-%m-%d")


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    stem = sanitize(stem)
    path = directory / f"{stem}{suffix}"
    i = 1
    while path.exists():
        path = directory / f"{stem}_{i}{suffix}"
        i += 1
    return path


def make_output_path(input_path: Path, tag: str = "enhanced") -> Path:
    """Build a deterministic output path.

    With date_folder enabled, outputs land in output_root/YYYY-MM-DD/.
    Name format: <原文件名>_<模型标签>.<ext>  (conflicts get _1, _2, ...)
    """
    cfg = load()
    folder = day_dir() if cfg.get("date_folder", True) else output_root()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{input_path.stem}_{tag}"
    return unique_path(folder, stem, input_path.suffix.lower())


def video_output_path(input_path: Path, tag: str = "enhanced") -> Path:
    cfg = load()
    folder = day_dir() if cfg.get("date_folder", True) else output_root()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{input_path.stem}_{tag}"
    return unique_path(folder, stem, ".mp4")
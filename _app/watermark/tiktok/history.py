"""
历史记录管理 (v3.1)
- 仅下载成功的任务才写入
- 字段: title, author, video_id, file_path, thumbnail, platform, content_type, file_size
"""
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from .models import DownloadTask, DownloadStatus, VideoInfo
from .config import HISTORY_FILE
from .logger import log


class HistoryManager:
    """下载历史 / 素材库"""

    def __init__(self, max_records: int = 1000):
        self.max_records = max_records
        self.records: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if HISTORY_FILE.exists():
            try:
                self.records = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"历史文件损坏: {e},重建空记录", exc_info=True)
                try:
                    shutil.copy2(HISTORY_FILE, HISTORY_FILE.with_suffix(".json.bak"))
                except Exception:
                    pass
                self.records = []

    def _save(self):
        HISTORY_FILE.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )

    def add(self, task: DownloadTask):
        """仅在下载成功时调用,失败的任务不写入"""
        if task.status != DownloadStatus.COMPLETED:
            return
        if not task.file_path:
            return
        if not Path(task.file_path).exists():
            return

        # Dedup by task_id so re-downloading the same batch does not
        # create duplicate history entries.
        if any(r.get("task_id") == task.task_id for r in self.records):
            return

        # 文件大小
        try:
            file_size = Path(task.file_path).stat().st_size
        except Exception:
            file_size = 0

        record = {
            "task_id": task.task_id,
            "url": task.info.url,
            "title": task.info.title,
            "author": task.info.author,
            "video_id": task.info.video_id,
            "file_path": task.file_path,
            "status": task.status.value,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "downloaded_at": datetime.now().isoformat(),
            # 素材库字段 (用户报告里要求的)
            "thumbnail": task.info.thumbnail,
            "platform": task.info.extra.get("platform", "unknown"),
            "content_type": task.info.content_type.value,
            "duration": task.info.duration,
            "view_count": task.info.view_count,
            "like_count": task.info.like_count,
            "file_size": file_size,
        }
        self.records.insert(0, record)
        self.records = self.records[:self.max_records]
        try:
            self._save()
        except Exception as e:
            from .logger import log
            log.warning(f"[history] 写入失败: {e}")

    def get_all(self) -> List[Dict[str, Any]]:
        return self.records

    def clear(self):
        self.records = []
        self._save()

    def remove(self, task_id: str):
        self.records = [r for r in self.records if r.get("task_id") != task_id]
        self._save()

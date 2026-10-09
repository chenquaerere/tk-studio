"""
数据模型定义
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
from datetime import datetime


class ContentType(Enum):
    """内容类型"""
    VIDEO = "video"
    IMAGE = "image"      # 图集 (TikTok 的 photo mode)
    UNKNOWN = "unknown"


class DownloadStatus(Enum):
    """下载状态"""
    PENDING = "等待中"
    PARSING = "解析中"
    DOWNLOADING = "下载中"
    COMPLETED = "已完成"
    FAILED = "失败"
    CANCELLED = "已取消"
    PAUSED = "已暂停"


@dataclass
class VideoInfo:
    """视频元数据"""
    url: str                          # 原始链接
    video_id: str = ""                # 视频 ID
    title: str = ""                   # 标题
    author: str = ""                  # 作者
    author_id: str = ""               # 作者 ID
    duration: float = 0.0            # 时长(秒)
    thumbnail: str = ""               # 缩略图 URL
    content_type: ContentType = ContentType.VIDEO
    download_url: str = ""           # 无水印下载地址
    image_urls: List[str] = field(default_factory=list)   # 图集地址
    music_url: str = ""              # 背景音乐
    music_title: str = ""
    create_time: int = 0
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    view_count: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class DownloadTask:
    """下载任务"""
    task_id: str                              # 任务 ID(唯一)
    info: VideoInfo                            # 视频信息
    output_dir: str = ""                       # 输出目录
    filename: str = ""                         # 文件名(不含后缀)
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0                     # 0-100
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: str = ""                            # 速度显示
    eta: str = ""                              # 预计剩余时间
    file_path: str = ""                        # 最终保存路径
    error: str = ""                            # 错误信息
    created_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None

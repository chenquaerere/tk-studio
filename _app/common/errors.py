"""类型化错误体系。

统一约定：
  - 每个错误都有稳定的 `code`，服务层把它透出给前端做文案映射；
  - 绝不把堆栈或内部细节返回给客户端。

用法：
    raise NotFoundError("账号不存在")
"""
from __future__ import annotations

from typing import Any, Optional


class AppError(Exception):
    """应用错误基类。"""

    code = "app_error"
    http_status = 500
    message = "内部错误"

    def __init__(self, message: Optional[str] = None,
                 *, detail: Any = None, **extra: Any):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict:
        out: dict = {"ok": False, "code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        if self.extra:
            out.update(self.extra)
        return out


class ConfigError(AppError):
    code = "config_error"
    http_status = 500
    message = "配置读取失败"


class ValidationError(AppError):
    code = "validation_error"
    http_status = 400
    message = "参数不合法"


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    message = "资源不存在"


class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    message = "资源冲突"


class PermissionDeniedError(AppError):
    code = "permission_denied"
    http_status = 403
    message = "操作被拒绝"


class DependencyError(AppError):
    """外部依赖不可用（模型缺失、ffmpeg 缺失、浏览器未安装等）。"""

    code = "dependency_error"
    http_status = 503
    message = "依赖不可用"


class JobError(AppError):
    code = "job_error"
    http_status = 500
    message = "任务执行失败"


class CancelledError(AppError):
    """用户主动取消（不是故障，调用方通常直接静默处理）。"""

    code = "cancelled"
    http_status = 200
    message = "任务已取消"


__all__ = [
    "AppError", "ConfigError", "ValidationError", "NotFoundError",
    "ConflictError", "PermissionDeniedError", "DependencyError", "JobError",
    "CancelledError",
]

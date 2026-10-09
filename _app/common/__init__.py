"""共享层：全应用通用的路径、配置、版本、错误体系。

本包不依赖任何业务内核（collector / watermark / server），
只能被它们依赖，不得反向依赖，避免循环导入。
"""
from . import errors, paths, version  # noqa: F401

__all__ = ["errors", "paths", "version", "config", "logging_setup"]


def __getattr__(name: str):
    """按需导入较重的子模块（config 会读写磁盘，避免仅 import 就产生副作用）。"""
    if name in ("config", "logging_setup"):
        import importlib
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

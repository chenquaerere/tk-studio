"""采集器配置适配层 —— 统一配置（common.config）的 collector 分区视图。

背景
----
采集器原本有自己的 `app/config.py`：一个"自带默认值 + 独立 config.json"的小管理器。
合并后全应用只有**一个** `config.json`，按 `{app, collector, watermark, hd}` 四分区存放。

本模块保留原 `Config` 的**全部对外接口**（get/set/save/load/as_dict/proxy_config），
读写落到 `collector` 分区，于是采集内核（engine / services / chat）无需任何改动。

    old:  Config("<base>/config.json").get("max_retry")
    new:  Config().get("max_retry")            # 同一份数据，来自 config.json["collector"]

线程/进程安全由 common.config 内部锁保证。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from common import config as _uc
from common import proxy as _proxy

DEFAULTS: Dict[str, Any] = _uc.DEFAULTS["collector"]


class Config:
    """采集器分区配置视图（接口与旧 app/config.py 完全一致）。"""

    def __init__(self, path: Optional[str] = None):
        # path 参数仅为兼容旧调用签名而保留（旧装配层会传 <base>/config.json）。
        # 统一配置模式下路径由 common.paths 决定，这里忽略它。
        self.path = path
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self._data.update(_uc.section("collector"))

    # ---- 读写 ----
    def load(self, path: Optional[str] = None) -> None:
        """（重新）载入 collector 分区。"""
        data = dict(DEFAULTS)
        data.update(_uc.section("collector"))
        self._data = data

    def save(self, path: Optional[str] = None) -> None:
        """把内存中的键写回 collector 分区（合并写入，不影响其它分区）。"""
        _uc.update("collector", self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update_many(self, values: Dict[str, Any]) -> None:
        """批量写入（新增便利方法，旧代码不曾使用）。"""
        self._data.update(values)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def proxy_config(self) -> Optional[Dict[str, Any]]:
        """返回 Playwright 格式的代理配置；未启用/未配置返回 None。

        代理由用户配置（proxy_enabled/proxy_type/proxy_host/proxy_port，
        以及 2026-10-13 新增的 proxy_username/proxy_password）。
        拼接逻辑统一走 `common.proxy.build()` —— 它同时容忍用户把
        `http://host:port` 整串填进「主机」框这类写法。

        安全：返回值**含密码**，只允许交给 Playwright。要写日志/回前端
        请先过 `common.proxy.display()`。本方法不硬编码任何地址。
        """
        if not self.get("proxy_enabled"):
            return None
        return _proxy.build(self.get("proxy_type"),
                            self.get("proxy_host"), self.get("proxy_port"),
                            self.get("proxy_username"), self.get("proxy_password"))

    @staticmethod
    def resolve_proxy(cfg) -> Optional[Dict[str, Any]]:
        """兼容旧调用：从任意带 proxy_config() 的对象或 dict 取代理。

        用途：服务层可能直接持有分区字典而非 Config 实例。
        """
        if cfg is None:
            return None
        if hasattr(cfg, "proxy_config"):
            return cfg.proxy_config()
        if isinstance(cfg, dict):
            if not cfg.get("proxy_enabled"):
                return None
            return _proxy.build(cfg.get("proxy_type"),
                                cfg.get("proxy_host"), cfg.get("proxy_port"),
                                cfg.get("proxy_username"), cfg.get("proxy_password"))
        return None

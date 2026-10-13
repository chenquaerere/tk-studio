"""代理配置构造、解析、脱敏与探测（共用层，零新依赖）。

定位
----
合并后代理有**多个使用点**（采集引擎 `bootstrap.py`、聊天搜索 `chat_service.py`、
聊天登录浏览器）与**多个配置入口**（设置页、HTTP API）。拼接 / 解析 / 脱敏 / 探测
全部收敛到本模块 —— 任何地方都不得再各写一份，否则三处必然漂移（铁律 24 的代理版）。

安全约定
--------
密码只允许出现在两处：① 交给 Playwright 的 dict；② 本机 `config.json`。
其余任何场合（日志、API 响应、异常文本、界面回显）一律走 `mask()` / `display()`。

零新依赖
--------
HTTP/HTTPS 代理的转发验证走既有依赖 `requests`；
SOCKS5 的转发验证用手写握手（`socket` + `struct`，约 40 行）——
不引入 PySocks，避免为一次性连通性检查增加安装负担。
"""
from __future__ import annotations

import os
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

# 支持的代理协议。socks4 无认证且极少使用，转发验证降级为 TCP 连通。
SCHEMES = ("http", "https", "socks5", "socks4")

# 无端口时的兜底端口（仅用于「整段粘贴」缺端口的情形，会给出提示）
_DEFAULT_PORT = {"http": "8080", "https": "443", "socks5": "1080", "socks4": "1080"}

# 探测目标：优先能回「出口 IP」的；都不通再退到只判连通的 204 探针。
_IP_ECHO_URLS = (
    "https://api.ipify.org?format=json",
    "http://ip-api.com/json/?fields=query",
    "https://ifconfig.me/ip",
)
_CONNECTIVITY_URLS = (
    "http://www.gstatic.com/generate_204",
    "http://cp.cloudflare.com/generate_204",
)
_PROBE_UA = "TKStudio-proxy-check/1.0"
# SOCKS5 是裸 TCP 隧道，没法借用 requests —— 自己拼一个最小 GET 探针。
_PROBE_PATH = "/generate_204"

# 本机常见代理端口（Windows 上真实会遇到的）。TCP 快筛后才会做转发验证。
_COMMON_PORTS: Tuple[Tuple[str, str, str], ...] = (
    ("http", "7890", "Clash / Clash for Windows"),
    ("socks5", "7891", "Clash SOCKS"),
    ("http", "7897", "Clash Verge"),
    ("socks5", "7899", "Clash Verge SOCKS"),
    ("http", "9910", "Clash.Meta / mihomo"),
    ("socks5", "10808", "v2rayN / Xray SOCKS"),
    ("http", "10809", "v2rayN / Xray HTTP"),
    ("socks5", "1080", "SOCKS 通用"),
    ("socks5", "1081", "SOCKS 备用"),
    ("http", "2080", "V2Ray / 其他客户端"),
    ("http", "8889", "通用 HTTP 代理"),
    ("http", "8080", "通用 HTTP 代理"),
    ("http", "8888", "通用 HTTP 代理"),
    ("http", "20171", "Shadowsocks-Windows 插件"),
    ("socks5", "4780", "Shadowsocks-Windows"),
    ("http", "33210", "迅雷 / 下载器"),
    ("socks5", "9050", "Tor"),
)

_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
             "ALL_PROXY", "all_proxy")


class ProxyError(Exception):
    """代理探测过程中可预期的失败（消息面向最终用户，已是可读中文）。"""


# --------------------------------------------------------------- 基础构造


def normalize_type(value: Any) -> str:
    """把用户/配置里的类型写法收敛到 SCHEMES 之一。"""
    t = str(value or "http").strip().lower()
    alias = {"socks": "socks5", "socks5h": "socks5", "sock5": "socks5",
             "socks4a": "socks4", "http-proxy": "http", "https-proxy": "https"}
    t = alias.get(t, t)
    return t if t in SCHEMES else "http"


def _split_host_port(raw: str) -> Tuple[str, str]:
    """从 `host` / `host:port` / `scheme://host:port` 里拆出主机与端口。

    用户极容易把整串 `http://127.0.0.1:7890` 直接填进「主机」框 ——
    旧实现会拼成 `http://http://127.0.0.1:7890:7890` 而必然失败，
    这里统一容错。
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if "://" in text:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = str(parts.port or "")
        return host, port
    # 形如 user:pass@host:port 的残片也一并剥掉
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if text.startswith("[") and "]" in text:      # IPv6 字面量
        host, _, tail = text.partition("]")
        tail = tail.lstrip(":")
        return host.lstrip("["), tail
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host.strip(), port.strip()
    return text.strip(), ""


def build(ptype: Any, host: Any, port: Any,
          username: Any = "", password: Any = "") -> Optional[Dict[str, Any]]:
    """构造 Playwright 可直接使用的代理 dict；信息不足返回 None。

    返回值形如 ``{"server": "http://127.0.0.1:7890", "username": "...", "password": "..."}``，
    username/password 仅在填了用户名时出现（与 Playwright 语义一致）。
    """
    h, p = _split_host_port(host)
    if not p:
        p = str(port or "").strip()
    if not h or not p:
        return None
    p = p.strip()
    if not p.isdigit() or not (0 < int(p) <= 65535):
        return None
    cfg: Dict[str, Any] = {"server": f"{normalize_type(ptype)}://{h}:{p}"}
    user = str(username or "").strip()
    if user:
        cfg["username"] = user
        cfg["password"] = str(password or "")
    return cfg


def parse(raw: Any) -> Dict[str, Any]:
    """把用户粘贴的**整段地址**解析成配置键。

    容忍下列真实写法（任一不合法则抛 ``ProxyError``，消息可读）：:

        127.0.0.1:7890
        127.0.0.1
        http://127.0.0.1:7890
        socks5://127.0.0.1:1080
        http://user:pass@1.2.3.4:8080
        user:pass@1.2.3.4:8080
        用户名:密码@主机:端口      （中文账号密码也可）

    返回 ``{"proxy_type","proxy_host","proxy_port","proxy_username","proxy_password","note"}``。
    注意：端口缺失时按类型给兜底端口，并在 ``note`` 里说明。
    """
    text = str(raw or "").strip()
    if not text:
        raise ProxyError("地址为空：请粘贴形如 http://user:pass@127.0.0.1:7890 的完整代理地址")

    ptype = "http"
    user = pw = ""
    note = ""

    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme:
            ptype = normalize_type(parts.scheme)
        user = unquote(parts.username or "")
        pw = unquote(parts.password or "")
        host = parts.hostname or ""
        port = str(parts.port or "")
    else:
        body = text
        if "@" in body:
            cred, _, body = body.rpartition("@")
            user, _, pw = cred.partition(":")
        host, port = _split_host_port(body)

    host = (host or "").strip()
    if not host:
        raise ProxyError(f"无法识别代理地址：{text[:60]}")

    if not port:
        port = _DEFAULT_PORT.get(ptype, "8080")
        note = f"原地址未带端口，已按 {ptype.upper()} 默认端口 {port} 填入，请确认是否正确"

    if not port.isdigit() or not (0 < int(port) <= 65535):
        raise ProxyError(f"端口不合法：{port!r}（应为 1–65535 的数字）")

    return {
        "proxy_type": ptype,
        "proxy_host": host,
        "proxy_port": port,
        "proxy_username": user,
        "proxy_password": pw,
        "note": note,
    }


# --------------------------------------------------------------- 脱敏展示


def display(cfg: Optional[Dict[str, Any]]) -> str:
    """脱敏后的单行展示串，**可以**安全写日志 / 回前端。"""
    if not cfg:
        return "(未配置)"
    server = str(cfg.get("server") or "")
    user = cfg.get("username")
    if not user:
        return server
    scheme, _, tail = server.partition("://")
    return f"{scheme}://{user}:***@{tail or server}"


def mask(value: Any) -> str:
    """把任意文本里 ``scheme://user:pass@host`` 形态的密码替换成 ``***``。

    用于兜底：任何"要把代理相关字符串写进日志"的地方都先过一遍它，
    避免将来某处不小心把带密码的 URL 直接打出去。
    """
    import re

    text = str(value or "")
    return re.sub(r"(?<=://)([^:/@\s]+):([^@/\s]+)(?=@)", r"\1:***", text)


def to_requests_url(cfg: Dict[str, Any]) -> str:
    """转成 requests 用的代理 URL（含认证；仅供内网探测使用，勿写日志）。"""
    server = str(cfg.get("server") or "")
    user = str(cfg.get("username") or "")
    if not user:
        return server
    scheme, _, tail = server.partition("://")
    pw = str(cfg.get("password") or "")
    return f"{scheme}://{quote(user, safe='')}:{quote(pw, safe='')}@{tail}"


# --------------------------------------------------------------- 错误翻译


def describe_error(exc: BaseException, host: str = "", port: Any = "") -> str:
    """把底层网络异常翻成用户能看懂的中文（含下一步该做什么）。"""
    if isinstance(exc, ProxyError):
        return str(exc)

    text = str(exc or "")
    low = text.lower()
    target = f"{host}:{port}" if host else "代理"

    if "getaddrinfo" in low or "name or service not known" in low or "11001" in text:
        return f"DNS 解析失败：找不到主机 {host or '(空)'}（地址是否写错？本地代理应填 127.0.0.1）"
    if "refused" in low or "10061" in text:
        return f"连接被拒绝：{target} 上没有服务在监听（代理软件没开，或端口填错了）"
    if "timed out" in low or "timeout" in low or "10060" in text:
        return f"连接超时：{target} 无响应（代理软件卡住、地址不可达，或被防火墙拦截）"
    if "unreachable" in low or "10065" in text or "10051" in text:
        return f"网络不可达：{target}（检查本机网络，或该地址是否只允许内网访问）"
    if "407" in text or "proxy authentication required" in low:
        return "代理要求认证（HTTP 407）：请在「用户名 / 密码」里填入代理商给的账号密码"
    if "407" in text:
        return "代理要求认证（HTTP 407）：请填写代理账号与密码"
    if "403" in text or "forbidden" in low:
        return "代理拒绝转发（403）：通常是出口 IP 未加入代理商白名单，或该套餐不允许访问此目标"
    if "502" in text or "503" in text or "504" in text:
        return "代理线路故障（502/503）：代理商侧节点异常，换一个节点或稍后重试"
    if "certificate" in low or "ssl" in low or "tls" in low:
        return f"TLS 握手失败：{text[:80]}（若代理只支持明文 HTTP，请把「类型」改成 HTTP）"
    if "connection reset" in low or "10054" in text:
        return (f"连接被对端重置：{target} 可能不是代理端口（被别的程序占用了），"
                "或代理规则拦截了探测目标")
    if "malformed" in low or "not a proxy" in low or "bad status line" in low:
        return f"{target} 响应不是代理协议：端口可能被别的程序占用了"
    return f"{type(exc).__name__}: {text[:120]}" if text else type(exc).__name__


# --------------------------------------------------------------- 候选发现


def _candidate(source: str, ptype: str, host: str, port: Any,
               label: str, username: str = "", password: str = "") -> Optional[Dict[str, Any]]:
    cfg = build(ptype, host, port, username, password)
    if not cfg:
        return None
    out = {
        "source": source,
        "label": label,
        "proxy_type": normalize_type(ptype),
        "proxy_host": str(host).strip(),
        "proxy_port": str(port).strip(),
        "proxy_username": str(username or ""),
        "proxy_password": str(password or ""),
        "display": display(cfg),
    }
    return out


def env_candidates() -> List[Dict[str, Any]]:
    """读环境变量里的代理（大小写都认，这是跨平台最通用的一种）。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    for key in _ENV_KEYS:
        raw = os.environ.get(key)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            parsed = parse(raw)
        except ProxyError:
            continue
        item = _candidate("env", parsed["proxy_type"], parsed["proxy_host"],
                          parsed["proxy_port"], f"环境变量 {key}",
                          parsed["proxy_username"], parsed["proxy_password"])
        if item:
            out.append(item)
    return out


def system_candidates() -> Tuple[List[Dict[str, Any]], str]:
    """读 Windows 系统代理（HKCU\\...\\Internet Settings）。

    返回 ``(候选列表, PAC 地址)``。PAC 只能提示、无法自动执行（需要 JS 引擎
    按 URL 求值），因此单独回传让界面告诉用户去代理软件里查端口。
    """
    out: List[Dict[str, Any]] = []
    pac = ""
    try:
        import winreg
    except ImportError:      # 非 Windows
        return out, pac

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            def _read(name):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return None

            enabled = _read("ProxyEnable")
            server = _read("ProxyServer") or ""
            pac = str(_read("AutoConfigURL") or "")
    except OSError:
        return out, pac

    if not enabled:
        return out, pac

    # 取值形态： "127.0.0.1:7890" 或 "http=127.0.0.1:7890;https=127.0.0.1:7890"
    if "=" not in str(server):
        item = _candidate("system", "http", *_split_host_port(str(server)),
                          "Windows 系统代理")
        if item:
            out.append(item)
        return out, pac

    for chunk in str(server).split(";"):
        proto, _, addr = chunk.partition("=")
        proto = proto.strip().lower()
        if not addr:
            continue
        ptype = "socks5" if proto.startswith("socks") else ("https" if proto == "https" else "http")
        host, port = _split_host_port(addr)
        item = _candidate("system", ptype, host, port, f"Windows 系统代理（{proto}）")
        if item:
            out.append(item)
    return out, pac


def scan_candidates() -> List[Dict[str, Any]]:
    """扫描本机常见代理端口 —— 只做 0.25 秒 TCP 快筛，不在这里做转发验证。"""
    out: List[Dict[str, Any]] = []
    for ptype, port, label in _COMMON_PORTS:
        if not _tcp_alive("127.0.0.1", port, 0.25):
            continue
        for candidate_type in (ptype, "socks5" if ptype == "http" else "http"):
            item = _candidate("scan", candidate_type, "127.0.0.1", port, label)
            if item:
                out.append(item)
    return out


def _tcp_alive(host: str, port: Any, timeout: float) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _tcp_precheck(host: str, port: Any, timeout: float) -> None:
    """快速 TCP 预检：端口不通时立刻抛出可读原因。

    没有这一步的话，「端口填错」这种 1 秒内就能知道的事会被拖成十几秒 ——
    因为 `_probe_http` 会把 3 个出口 IP 站点 + 2 个连通性站点各超时一遍，
    用户看着"测试中…"转半天才得到一句"连接被拒绝"。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return
    except OSError as exc:
        raise ProxyError(describe_error(exc, host, port)) from exc


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        key = (it["proxy_type"], it["proxy_host"], it["proxy_port"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def discover() -> Dict[str, Any]:
    """汇总所有来源的候选（**只读，不改任何配置**）。"""
    env_items = env_candidates()
    sys_items, pac = system_candidates()
    scan_items: List[Dict[str, Any]] = []
    try:
        scan_items = scan_candidates()
    except Exception:                                   # noqa: BLE001
        scan_items = []
    items = _dedupe(env_items + sys_items + scan_items)
    for it in items:
        it.setdefault("ok", None)
        it.setdefault("exit_ip", "")
        it.setdefault("latency_ms", None)
        it.setdefault("error", "")
    return {"candidates": items, "pac_url": pac}


# --------------------------------------------------------------- 转发验证


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("代理在握手过程中断开了连接")
        buf += chunk
    return buf


_SOCKS5_REPLY = {
    1: "代理服务器内部故障", 2: "代理规则不允许连接该目标", 3: "网络不可达",
    4: "目标主机不可达", 5: "目标拒绝连接", 6: "TTL 超时",
    7: "代理不支持该命令", 8: "代理不支持该地址类型",
}


def _socks5_roundtrip(cfg: Dict[str, Any], target_host: str, target_port: int,
                      timeout: float) -> bytes:
    """手写 SOCKS5 握手并取回一次 HTTP 响应（零依赖，避免引入 PySocks）。"""
    parts = urlsplit(str(cfg.get("server") or ""))
    proxy_host = parts.hostname or ""
    proxy_port = int(parts.port or 1080)
    user = str(cfg.get("username") or "")
    pw = str(cfg.get("password") or "")

    with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        head = _recv_exact(sock, 2)
        if head[0] != 5:
            raise ProxyError(f"{proxy_host}:{proxy_port} 不是 SOCKS5 代理（应答版本 {head[0]}）")
        method = head[1]
        if method == 2:
            if not user:
                raise ProxyError("代理要求账号密码认证：请填写「用户名 / 密码」")
            ub, pb = user.encode("utf-8"), pw.encode("utf-8")
            if len(ub) > 255 or len(pb) > 255:
                raise ProxyError("SOCKS5 账号或密码过长（上限 255 字节）")
            sock.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
            if _recv_exact(sock, 2)[1] != 0:
                raise ProxyError("SOCKS5 认证失败：账号或密码不正确")
        elif method == 0xFF:
            raise ProxyError("代理拒绝了所有认证方式（可能只允许白名单 IP）")
        elif method != 0:
            raise ProxyError(f"代理要求不支持的认证方式（0x{method:02x}）")

        th = target_host.encode("idna") if not _is_ip(target_host) else target_host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(th)]) + th
                     + struct.pack(">H", target_port))
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise ProxyError(f"SOCKS5 建连失败：{_SOCKS5_REPLY.get(reply[1], '错误码 %d' % reply[1])}")
        atyp = reply[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 4:
            _recv_exact(sock, 16)
        _recv_exact(sock, 2)

        request = (f"GET {_PROBE_PATH} HTTP/1.1\r\nHost: {target_host}\r\n"
                   f"User-Agent: {_PROBE_UA}\r\nAccept: */*\r\n"
                   "Connection: close\r\n\r\n").encode()
        sock.sendall(request)
        data = b""
        while len(data) < 65536:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data


def _is_ip(text: str) -> bool:
    try:
        socket.inet_aton(text)
        return True
    except OSError:
        return False


def _extract_ip(body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    if body.startswith("{"):
        import json
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return str(data.get("query") or data.get("ip") or "").strip()
        except ValueError:
            return ""
    # 纯文本响应（ifconfig.me/ip）
    return body.split()[0] if body.split() else ""


def _probe_http(cfg: Dict[str, Any], timeout: float) -> Tuple[str, str]:
    """通过 HTTP/HTTPS 代理发一次真实请求，返回 (出口 IP, 说明)。"""
    import requests

    url = to_requests_url(cfg)
    proxies = {"http": url, "https": url}
    last_error: Optional[BaseException] = None

    for target in _IP_ECHO_URLS:
        try:
            resp = requests.get(target, proxies=proxies, timeout=timeout,
                                headers={"User-Agent": _PROBE_UA})
            if resp.status_code == 407:
                raise ProxyError("代理要求认证（HTTP 407）：请在「用户名 / 密码」里填入账号密码")
            if resp.status_code == 403:
                raise ProxyError("代理拒绝转发（403）：出口 IP 可能未加入代理商白名单")
            if resp.status_code >= 500:
                raise ProxyError(f"代理线路故障（HTTP {resp.status_code}）：代理商侧节点异常")
            if resp.status_code == 200:
                ip = _extract_ip(resp.text)
                if ip:
                    return ip, ""
        except ProxyError:
            raise
        except requests.exceptions.ProxyError as exc:
            text = str(exc)
            if "407" in text:
                raise ProxyError("代理要求认证（HTTP 407）：请在「用户名 / 密码」里填入账号密码") from exc
            last_error = exc
        except Exception as exc:                        # noqa: BLE001
            last_error = exc

    # 取不到出口 IP，退一步只验证「能否借代理连通外网」
    for target in _CONNECTIVITY_URLS:
        try:
            resp = requests.get(target, proxies=proxies, timeout=timeout,
                                headers={"User-Agent": _PROBE_UA})
            if resp.status_code in (200, 204):
                return "", "代理可转发流量（未能取到出口 IP，可能探测站点被该线路拦截）"
        except Exception as exc:                        # noqa: BLE001
            last_error = exc

    if last_error is not None:
        host, _, port = str(cfg.get("server") or "").split("://")[-1].rpartition(":")
        raise ProxyError(describe_error(last_error, host, port))
    raise ProxyError("代理未能成功转发任何请求")


def probe(candidate: Dict[str, Any], timeout: float = 6.0) -> Dict[str, Any]:
    """对单个候选做「真转发」验证，返回带 ok/exit_ip/latency_ms/error 的新 dict。"""
    item = dict(candidate)
    host = str(item.get("proxy_host") or "")
    port = item.get("proxy_port")
    ptype = normalize_type(item.get("proxy_type"))
    item["proxy_type"] = ptype

    cfg = build(ptype, host, port, item.get("proxy_username"), item.get("proxy_password"))
    if cfg is None:
        item.update(ok=False, error="地址或端口不完整")
        return item

    started = time.time()
    try:
        # 预检最多 4 秒：本地端口通常毫秒级返回，只有填了远端地址才可能等满。
        _tcp_precheck(host, port, min(float(timeout), 4.0))
        if ptype in ("http", "https"):
            exit_ip, note = _probe_http(cfg, timeout)
        elif ptype == "socks5":
            raw = _socks5_roundtrip(cfg, "www.gstatic.com", 80, timeout)
            head = raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 204" not in head and " 200" not in head:
                raise ProxyError(f"SOCKS5 可建连但目标返回异常：{head[:60] or '无响应'}")
            exit_ip, note = "", "SOCKS5 可正常转发（出口 IP 需在浏览器中确认）"
        else:                                           # socks4：仅 TCP 连通
            exit_ip, note = "", "socks4 仅验证端口连通（该协议已很少使用）"
        item.update(ok=True, exit_ip=exit_ip, note=note,
                    latency_ms=int((time.time() - started) * 1000), error="")
    except ProxyError as exc:
        item.update(ok=False, latency_ms=int((time.time() - started) * 1000),
                    error=str(exc))
    except Exception as exc:                            # noqa: BLE001
        item.update(ok=False, latency_ms=int((time.time() - started) * 1000),
                    error=describe_error(exc, host, port))
    return item


def probe_many(candidates: List[Dict[str, Any]], timeout: float = 6.0,
               workers: int = 6) -> List[Dict[str, Any]]:
    """并发验证多个候选（保持输入顺序）。"""
    if not candidates:
        return []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
        return list(pool.map(lambda c: probe(c, timeout), candidates))


def probe_config(cfg: Optional[Dict[str, Any]], timeout: float = 6.0,
                 fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """验证一份「已构造好的」代理配置（供测试端点直接用表单值调用）。"""
    base = fallback or {}
    host = str(base.get("proxy_host") or "")
    port = base.get("proxy_port")
    ptype = normalize_type(base.get("proxy_type"))
    if cfg is None:
        return {"ok": False, "message": "未启用代理或未填写地址", "stage": "config"}
    item = {"source": "manual", "label": "当前填写", "proxy_type": ptype,
            "proxy_host": host, "proxy_port": port,
            "proxy_username": base.get("proxy_username", ""),
            "proxy_password": base.get("proxy_password", ""),
            "display": display(cfg)}
    return probe(item, timeout)

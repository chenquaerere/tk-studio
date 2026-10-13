"""自签 TLS 证书的生成与复用（共用层，零新依赖）。

为什么需要它
------------
手机的「长按 → 存储到照片」只能逐张，而**一键存相册 / 直接分享到抖音**靠的是
Web Share API（`navigator.share`）。该 API 带 `[SecureContext]` 标记：
**在非 localhost 的 http:// 源上直接是 `undefined`** —— 而手机访问本程序用的正是
`http://192.168.x.x:端口`。所以要让分享/保存按钮可用，就必须先有 HTTPS。

为什么不用 cryptography / trustme
---------------------------------
两者在本项目的运行环境里都**没有安装**，而本程序的交付方式是「拷贝源文件覆盖」
（增量更新包），装新依赖会让另一台电脑必须联网 pip install。
本项目已有同类先例（铁律 26：SOCKS5 手写握手，不引 PySocks）——
这里同样**手写 ASN.1 DER + RSA 密钥对 + X.509 v3 自签证书**，只用标准库
（`secrets` / `hashlib` / `base64` / `socket`），约 300 行。

iOS 的三条硬要求（踩坑点，改这里前必读）
----------------------------------------
1. **必须有 SAN**（subjectAltName）。iOS 13 起只看 SAN，**不看 CN**；
   而且用 IP 直连时，IP 必须以 **iPAddress 类型**（tag 0x87）写进 SAN，
   写成 dNSName 一律不认。所以证书里会同时放 `localhost`、局域网 IP、
   主机名与 `<主机名>.local`。
2. **有效期 ≤ 825 天**（Apple 自 2019-07-01 起对 TLS 服务器证书的限制）。
   这里取 820 天，留出余量。
3. **签名算法 ≥ SHA-256、RSA ≥ 2048 位**。这里用 RSA-2048 + sha256WithRSAEncryption。

另外：自签证书必须让用户在手机上**两步**操作才生效 ——
① 设置 → 通用 → VPN与设备管理 → 安装描述文件；
② 设置 → 通用 → 关于本机 → 证书信任设置 → 打开「完全信任」。
少第二步则浏览器仍报「证书无效」。

产物（都在 runtime/tls/，已随 runtime 目录被 gitignore，不会进仓库）
-------------------------------------------------------------------
- `server.crt`  证书（PEM）
- `server.key`  私钥（PKCS#8 PEM，未加密）—— **绝不可提交、绝不可外传**
- `server.json` 元数据（SAN 列表 / 有效期 / 指纹），供界面展示；
  读取时会校验它与 crt 是否对得上，对不上就当作「无证书」
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths

# ---- 落盘位置 ----
TLS_DIR = paths.RUNTIME_DIR / "tls"
CERT_PATH = TLS_DIR / "server.crt"
KEY_PATH = TLS_DIR / "server.key"
META_PATH = TLS_DIR / "server.json"

# 有效期：Apple 要求 ≤ 825 天，取 820 留余量
VALID_DAYS = 820
# 起算时间往前挪一天，容忍手机与电脑的时钟偏差
SKEW_HOURS = 24
# 至少还剩这么多天才复用，否则重新签发
MIN_REMAIN_DAYS = 30

# ---- OID ----
_OID_RSA = "1.2.840.113549.1.1.1"
_OID_SHA256_RSA = "1.2.840.113549.1.1.11"
_OID_CN = "2.5.4.3"
_OID_O = "2.5.4.10"
_OID_BASIC_CONSTRAINTS = "2.5.29.19"
_OID_KEY_USAGE = "2.5.29.15"
_OID_EXT_KEY_USAGE = "2.5.29.37"
_OID_SAN = "2.5.29.17"
_OID_SKI = "2.5.29.14"
_OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"

# SHA-256 的 DigestInfo 前缀（RFC 8017 附录 A.2.4）
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class TLSError(RuntimeError):
    """证书生成 / 读取失败。"""


# ======================================================================
# ASN.1 DER 基础编码
# ======================================================================

def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(body)) + body


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _int(value: int) -> bytes:
    """INTEGER，正数用最简补码编码（前导 0 只在必要时代补符号位）。"""
    if value == 0:
        return _tlv(0x02, b"\x00")
    raw = value.to_bytes((value.bit_length() + 8) // 8, "big")
    while len(raw) > 1 and raw[0] == 0x00 and not (raw[1] & 0x80):
        raw = raw[1:]
    return _tlv(0x02, raw)


def _bool(value: bool) -> bytes:
    return _tlv(0x01, b"\xff" if value else b"\x00")


def _octet(data: bytes) -> bytes:
    return _tlv(0x04, data)


def _bits(data: bytes, unused: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused]) + data)


def _utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _utc(when: datetime) -> bytes:
    """UTCTime（YYMMDDHHMMSSZ）。2050 年前都够用。"""
    return _tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    if len(parts) < 2:
        raise TLSError(f"非法 OID: {dotted}")
    body = bytearray([40 * parts[0] + parts[1]])
    for value in parts[2:]:
        chunk = bytearray([value & 0x7F])
        value >>= 7
        while value:
            chunk.insert(0, 0x80 | (value & 0x7F))
            value >>= 7
        body += chunk
    return _tlv(0x06, bytes(body))


def _null() -> bytes:
    return b"\x05\x00"


def _ctx(num: int, body: bytes, constructed: bool = True) -> bytes:
    return _tlv((0xA0 if constructed else 0x80) | num, body)


# ======================================================================
# RSA 密钥对（纯 Python）
# ======================================================================

_SMALL_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47)


def _is_probable_prime(n: int, rounds: int = 48) -> bool:
    """Miller-Rabin 概率素性检验（先做小素数试除快速排除）。"""
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(cand):
            return cand


def rsa_keygen(bits: int = 2048) -> Dict[str, int]:
    """生成 RSA 密钥对（e 固定 65537）。2048 位在纯 Python 里约 0.2~2 秒。"""
    if bits % 8 or bits < 1024:
        raise TLSError(f"不支持的密钥长度: {bits}")
    e = 65537
    half = bits // 2
    while True:
        p = _gen_prime(half)
        q = _gen_prime(half)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        if math.gcd(e, (p - 1) * (q - 1)) != 1:
            continue
        lam = ((p - 1) * (q - 1)) // math.gcd(p - 1, q - 1)
        d = pow(e, -1, lam)
        return {
            "n": n, "e": e, "d": d, "p": p, "q": q,
            "dp": d % (p - 1), "dq": d % (q - 1), "qi": pow(q, -1, p),
        }


def _pkcs1_private_der(key: Dict[str, int]) -> bytes:
    return _seq(_int(0), _int(key["n"]), _int(key["e"]), _int(key["d"]),
                _int(key["p"]), _int(key["q"]), _int(key["dp"]),
                _int(key["dq"]), _int(key["qi"]))


def _pkcs8_private_der(key: Dict[str, int]) -> bytes:
    return _seq(_int(0),
                _seq(_oid(_OID_RSA), _null()),
                _octet(_pkcs1_private_der(key)))


def _spki_der(key: Dict[str, int]) -> bytes:
    return _seq(_seq(_oid(_OID_RSA), _null()),
                _bits(_seq(_int(key["n"]), _int(key["e"]))))


def _rsa_sign(key: Dict[str, int], message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 + SHA-256 签名，返回定长 n 字节。"""
    digest = hashlib.sha256(message).digest()
    digest_info = _SHA256_DIGEST_INFO + digest
    k = (key["n"].bit_length() + 7) // 8
    pad = k - len(digest_info) - 3
    if pad < 8:
        raise TLSError("模数太短，无法做 PKCS#1 v1.5 填充")
    em = b"\x00\x01" + b"\xff" * pad + b"\x00" + digest_info
    signed = pow(int.from_bytes(em, "big"), key["d"], key["n"])
    out = signed.to_bytes(k, "big")
    # 自检：用公钥验一次，防止 e/d/模数不一致却静默产出坏签名
    if pow(signed, key["e"], key["n"]) != int.from_bytes(em, "big"):
        raise TLSError("签名自检失败（密钥对不一致）")
    return out


def _pem(der: bytes, label: str) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def pem_to_der(text: str) -> bytes:
    """从 PEM 里取出 DER（用于算指纹）。非 PEM 一律当损坏处理。"""
    raw = str(text)
    if "-----BEGIN" not in raw or "-----END" not in raw:
        raise TLSError("不是 PEM 格式（缺少 BEGIN/END 标记）")
    lines = [ln.strip() for ln in raw.splitlines()
             if ln.strip() and not ln.strip().startswith("-----")]
    if not lines:
        raise TLSError("证书文件为空")
    try:
        return base64.b64decode("".join(lines), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise TLSError(f"证书 PEM 解码失败: {exc}") from exc


# ======================================================================
# X.509 v3 自签证书
# ======================================================================

def _name(common_name: str, org: str = "TK Studio") -> bytes:
    return _seq(
        _set(_seq(_oid(_OID_O), _utf8(org))),
        _set(_seq(_oid(_OID_CN), _utf8(common_name))),
    )


def _general_names(hosts: List[Tuple[str, str]]) -> bytes:
    """GeneralName 序列：('ip'|'dns', 值)。"""
    out: List[bytes] = []
    seen = set()
    for kind, value in hosts:
        value = str(value).strip()
        if not value or (kind, value) in seen:
            continue
        seen.add((kind, value))
        if kind == "ip":
            try:
                out.append(_tlv(0x87, socket.inet_aton(value)))
            except OSError:
                continue
        elif kind == "ip6":
            try:
                out.append(_tlv(0x87, socket.inet_pton(socket.AF_INET6, value)))
            except OSError:
                continue
        else:
            try:
                out.append(_tlv(0x82, value.encode("ascii")))
            except UnicodeEncodeError:
                continue
    if not out:
        raise TLSError("SAN 为空：至少需要一个 DNS 名或 IP")
    return _seq(*out)


def _extensions(hosts: List[Tuple[str, str]], key: Dict[str, int]) -> bytes:
    # 方法 1（RFC 5280）：keyIdentifier = SHA-1(SubjectPublicKey 的 DER)
    pub_der = _seq(_int(key["n"]), _int(key["e"]))
    ski = hashlib.sha1(pub_der).digest()  # noqa: S324 —— 只作标识符，非安全用途

    exts = [
        # CA:TRUE —— 它是自定义信任根；同时带 serverAuth，可直接当服务器证书用
        _seq(_oid(_OID_BASIC_CONSTRAINTS), _bool(True), _octet(_seq(_bool(True)))),
        # keyUsage: digitalSignature(0) + keyEncipherment(2) + keyCertSign(5)
        # → 0b10100100 = 0xA4，末字节尾随 2 个 0 位 ⇒ unused=2
        _seq(_oid(_OID_KEY_USAGE), _bool(True), _octet(_bits(b"\xa4", 2))),
        _seq(_oid(_OID_EXT_KEY_USAGE), _octet(_seq(_oid(_OID_SERVER_AUTH)))),
        _seq(_oid(_OID_SAN), _octet(_general_names(hosts))),
        _seq(_oid(_OID_SKI), _octet(_octet(ski))),
    ]
    return _seq(*exts)


def build_self_signed(hosts: List[Tuple[str, str]], *,
                      common_name: str = "TK Studio",
                      days: int = VALID_DAYS,
                      key: Optional[Dict[str, int]] = None,
                      key_bits: int = 2048) -> Dict[str, Any]:
    """生成自签证书。返回 {"cert_pem", "key_pem", "meta"}。"""
    hosts = list(hosts)
    if not hosts:
        raise TLSError("没有可用主机名 / IP")
    days = min(int(days), 825)          # Apple 的硬上限，不给自己找麻烦
    key = key or rsa_keygen(key_bits)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    not_before = now - timedelta(hours=SKEW_HOURS)
    not_after = not_before + timedelta(days=days)

    algo = _seq(_oid(_OID_SHA256_RSA), _null())
    name = _name(common_name)
    spki = _spki_der(key)

    tbs = _seq(
        _ctx(0, _int(2)),                                   # version v3
        _int(secrets.randbits(128) & ((1 << 127) - 1)),      # serialNumber（正）
        algo,                                                # signature
        name,                                                # issuer
        _seq(_utc(not_before), _utc(not_after)),             # validity
        name,                                                # subject
        spki,                                                # subjectPublicKeyInfo
        _ctx(3, _extensions(hosts, key)),                    # extensions
    )
    cert_der = _seq(tbs, algo, _bits(_rsa_sign(key, tbs)))

    meta = {
        "common_name": common_name,
        "hosts": [[k, v] for k, v in hosts],
        "not_before": not_before.strftime("%Y-%m-%d %H:%M:%S"),
        "not_after": not_after.strftime("%Y-%m-%d %H:%M:%S"),
        "valid_days": days,
        "key_bits": int(key["n"].bit_length()),
        "signature": "sha256WithRSAEncryption",
        "fingerprint_sha256": hashlib.sha256(cert_der).hexdigest(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {
        "cert_pem": _pem(cert_der, "CERTIFICATE"),
        "key_pem": _pem(_pkcs8_private_der(key), "PRIVATE KEY"),
        "meta": meta,
    }


# ======================================================================
# 路径 / 落盘 / 复用判断
# ======================================================================

def local_hosts() -> List[Tuple[str, str]]:
    """本机可用的访问名：localhost + 主机名 + **全部**内网 IPv4。

    为什么要放**全部**内网地址：这台机器上常有多块网卡（有线 / 无线 / 虚拟网卡），
    手机走哪一块完全取决于路由，只写一个 IP 时换块网卡就会报「证书无效」，
    而用户不可能猜到是这个原因。
    """
    hosts: List[Tuple[str, str]] = [("dns", "localhost"), ("ip", "127.0.0.1")]
    try:
        name = socket.gethostname()
        if name:
            hosts.append(("dns", name))
            hosts.append(("dns", f"{name}.local"))
    except Exception:  # noqa: BLE001
        pass
    for ip in local_ipv4s():
        hosts.append(("ip", ip))
    return hosts


def _is_private_ipv4(ip: str) -> bool:
    import re
    return bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", ip))


def local_ipv4s() -> List[str]:
    """本机所有内网 IPv4（顺序：默认路由出口优先）。"""
    found: List[str] = []
    primary = _udp_probe_ip()
    if primary:
        found.append(primary)
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(item[4][0])
            if _is_private_ipv4(ip) and ip not in found:
                found.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return found


def _udp_probe_ip() -> str:
    """UDP connect 让系统按路由表选出网卡（不真发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        return ""
    return ip if _is_private_ipv4(ip) else ""


def lan_ip() -> str:
    """界面上展示的「手机该用哪个 IP」——取默认路由出口那块网卡。"""
    ips = local_ipv4s()
    return ips[0] if ips else ""


def _parse_time(text: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text), fmt)
        except (ValueError, TypeError):
            continue
    return None


def cert_info() -> Dict[str, Any]:
    """读证书状态。``exists`` 为 False 时其余字段都是兜底值。

    会校验元数据与 crt 文件是否对得上（用户手动换过文件 / 拷贝错位都能识别）。
    """
    info: Dict[str, Any] = {
        "exists": False, "cert_path": str(CERT_PATH), "key_path": str(KEY_PATH),
        "fingerprint_sha256": "", "fingerprint_short": "",
        "not_before": "", "not_after": "", "days_left": 0,
        "hosts": [], "lan_ip": "", "local_ips": [], "ip_covered": False,
        "missed_ips": [], "ok": False, "reason": "尚未生成",
    }
    ips = local_ipv4s()
    info["lan_ip"] = ips[0] if ips else ""
    info["local_ips"] = ips
    if not (CERT_PATH.is_file() and KEY_PATH.is_file()):
        return info
    try:
        der = pem_to_der(CERT_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"证书损坏: {exc}"
        return info
    real_fp = hashlib.sha256(der).hexdigest()

    meta: Dict[str, Any] = {}
    if META_PATH.is_file():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    if meta.get("fingerprint_sha256") and meta["fingerprint_sha256"] != real_fp:
        info["reason"] = "证书已被替换，元数据不匹配（重新生成一次即可）"
        info["fingerprint_sha256"] = real_fp
        info["fingerprint_short"] = real_fp[:16].upper()
        return info

    hosts = meta.get("hosts") or []
    cert_ips = [str(v) for _k, v in hosts if _k == "ip"]
    info.update(
        exists=True,
        fingerprint_sha256=real_fp,
        fingerprint_short=real_fp[:16].upper(),
        not_before=str(meta.get("not_before") or ""),
        not_after=str(meta.get("not_after") or ""),
        hosts=hosts,
        key_bits=int(meta.get("key_bits") or 0),
        common_name=str(meta.get("common_name") or ""),
        created=str(meta.get("created") or ""),
    )
    after = _parse_time(info["not_after"])
    if after:
        info["days_left"] = max(0, (after - datetime.now()).days)
    missing = [ip for ip in ips if ip not in cert_ips]
    info["missed_ips"] = missing
    info["ip_covered"] = not missing
    if after and info["days_left"] <= 0:
        info["reason"] = "证书已过期，需要重新生成"
    elif missing:
        info["reason"] = (f"证书里没有 {'、'.join(missing)}"
                          "（换过网络或换了网卡？重新生成一次即可）")
    else:
        info["ok"] = True
        info["reason"] = "证书可用"
    return info


def ensure_cert(hosts: Optional[List[Tuple[str, str]]] = None,
                *, force: bool = False) -> Dict[str, Any]:
    """确保存在可用证书；不满足条件就重新签发。返回 ``cert_info()`` 的结果。

    复用条件（三个都满足才复用，否则重签）：
      ① 证书与私钥都在；② 剩余有效期 > ``MIN_REMAIN_DAYS``；
      ③ 当前局域网 IP 已在 SAN 里（换网络/换 WiFi 后 IP 会变，
         不变的话手机看到的还是「证书无效」，且用户根本不知道为什么）。
    """
    hosts = list(hosts or local_hosts())
    info = cert_info()
    if not force and info.get("exists"):
        fresh = int(info.get("days_left") or 0) > MIN_REMAIN_DAYS
        if fresh and info.get("ip_covered"):
            return info
    return generate(hosts)


def generate(hosts: Optional[List[Tuple[str, str]]] = None, *,
             common_name: str = "TK Studio") -> Dict[str, Any]:
    """（重新）签发证书并落盘。失败抛 TLSError。

    无论调用方传什么，都会**补齐本机当前所有内网 IPv4** ——
    这样「证书覆盖本机地址」是构造上就成立的，不会出现
    「传进来的 hosts 少了某个网卡 → 下次启动又判定要重签」的来回抖动。
    """
    hosts = list(hosts or local_hosts())
    have = {str(v) for k, v in hosts if k == "ip"}
    for ip in local_ipv4s():
        if ip not in have:
            hosts.append(("ip", ip))
    built = build_self_signed(hosts, common_name=common_name)
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    # newline=""：不要让 Windows 把 \n 翻成 \r\n（PEM 解析器通常容忍，
    # 但这个仓库已经被同类换行问题坑过一次，统一按写入内容原样落盘）
    KEY_PATH.write_text(built["key_pem"], encoding="ascii", newline="")
    CERT_PATH.write_text(built["cert_pem"], encoding="ascii", newline="")
    meta = dict(built["meta"])
    meta["lan_ip"] = lan_ip()
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8", newline="")
    try:
        os.chmod(KEY_PATH, 0o600)      # POSIX 下有效；Windows 上忽略即可
    except OSError:
        pass
    return cert_info()


def remove_cert() -> bool:
    """删除证书三件套（crt / key / meta）。返回是否删掉了至少一个文件。"""
    hit = False
    for p in (CERT_PATH, KEY_PATH, META_PATH):
        try:
            p.unlink(missing_ok=True)
            hit = True
        except OSError:
            pass
    return hit


def ssl_kwargs() -> Dict[str, str]:
    """给 uvicorn.Config 用的 ssl 参数。没证书就返回空字典（= 不走 TLS）。"""
    if CERT_PATH.is_file() and KEY_PATH.is_file():
        return {"ssl_certfile": str(CERT_PATH), "ssl_keyfile": str(KEY_PATH)}
    return {}

"""自签 HTTPS 证书（纯标准库生成）的正确性回归（v1.5.0）。

为什么值得单独测：`common/tls.py` 手写 ASN.1 DER + RSA + X.509 v3 ——
任何一处字节写错都可能「看起来像张证书，浏览器却不认」，而且错在哪
从报错信息里几乎看不出来（只会得到一句笼统的「证书无效」）。

这里用**标准库 `ssl`（底层就是 OpenSSL）真的加载一次证书链**：
能加载 = DER 结构、密钥配对、PEM 封装都对。不依赖 `cryptography`，
也不依赖外部的 openssl 命令，所以任何一台机器上都能跑。

全程把证书目录指到临时目录，绝不碰 `runtime/tls/` 里正在用的证书。
"""
import ssl
import tempfile
import unittest
from pathlib import Path

from common import tls


class TestSelfSignedCert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._orig = (tls.TLS_DIR, tls.CERT_PATH, tls.KEY_PATH, tls.META_PATH)
        tls.TLS_DIR = d
        tls.CERT_PATH = d / "server.crt"
        tls.KEY_PATH = d / "server.key"
        tls.META_PATH = d / "server.json"
        self.addCleanup(self._restore)

    def _restore(self):
        (tls.TLS_DIR, tls.CERT_PATH, tls.KEY_PATH, tls.META_PATH) = self._orig
        self._tmp.cleanup()

    def _san_ips(self, info):
        return {str(v) for k, v in info["hosts"] if k == "ip"}

    # ---------------------------------------------------------------- 生成

    def test_generated_cert_loads_in_openssl(self):
        """能通过 OpenSSL 加载 = DER 结构 / 密钥配对 / PEM 封装全对。"""
        info = tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        self.assertTrue(info["exists"])
        self.assertTrue(info["ok"], info["reason"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(tls.CERT_PATH), str(tls.KEY_PATH))  # 不抛即通过

    def test_validity_and_key_size_within_apple_limits(self):
        """Apple 的硬要求：有效期 ≤ 825 天、RSA ≥ 2048、SHA-256 签名。"""
        info = tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        self.assertLessEqual(info["days_left"], 825)
        self.assertGreater(info["days_left"], 800)
        self.assertGreaterEqual(info["key_bits"], 2048)

    def test_san_contains_every_local_ip(self):
        """iOS 13 起只看 SAN 且 IP 必须是 iPAddress 类型，只看 CN 一律不认。"""
        info = tls.generate()
        san = self._san_ips(info)
        self.assertIn("127.0.0.1", san)
        self.assertTrue(info["local_ips"], "本机居然一个内网 IP 都没有？")
        for ip in info["local_ips"]:
            self.assertIn(ip, san, f"内网 IP {ip} 没写进 SAN")

    def test_private_key_is_pkcs8(self):
        tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        key = tls.KEY_PATH.read_text(encoding="ascii")
        crt = tls.CERT_PATH.read_text(encoding="ascii")
        self.assertTrue(key.startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertNotIn("RSA PRIVATE KEY", key)          # 不要过时的 PKCS#1 头
        self.assertTrue(crt.startswith("-----BEGIN CERTIFICATE-----"))

    # ------------------------------------------------------ 复用 / 重新签发

    def test_reuse_when_nothing_changed(self):
        first = tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        again = tls.ensure_cert([("dns", "localhost"), ("ip", "127.0.0.1")])
        self.assertEqual(again["fingerprint_sha256"], first["fingerprint_sha256"])

    def test_regenerates_when_lan_ip_changed(self):
        """换了网络/网卡 → SAN 里没有新 IP → 必须自动重签，否则手机报证书无效。"""
        orig = tls.local_ipv4s
        try:
            tls.local_ipv4s = lambda: ["192.168.5.5"]
            first = tls.generate()
            self.assertTrue(first["ip_covered"], first["reason"])

            tls.local_ipv4s = lambda: ["192.168.9.9"]
            info = tls.cert_info()
            self.assertFalse(info["ip_covered"])
            self.assertIn("重新生成", info["reason"])

            again = tls.ensure_cert()
            self.assertTrue(again["ip_covered"], again["reason"])
            self.assertNotEqual(again["fingerprint_sha256"],
                                first["fingerprint_sha256"])
        finally:
            tls.local_ipv4s = orig

    def test_generate_tops_up_local_ips(self):
        """即使调用方只给了 localhost，也要把本机内网 IP 补进 SAN。"""
        info = tls.generate([("dns", "localhost")])
        for ip in info["local_ips"]:
            self.assertIn(ip, self._san_ips(info))

    # ---------------------------------------------------------- 异常与降级

    def test_corrupted_cert_reports_instead_of_raising(self):
        tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        tls.CERT_PATH.write_text("这根本不是证书", encoding="utf-8")
        info = tls.cert_info()               # 不能抛
        self.assertFalse(info["exists"])
        self.assertIn("损坏", info["reason"])

    def test_missing_cert_is_reported_plainly(self):
        info = tls.cert_info()
        self.assertFalse(info["exists"])
        self.assertFalse(info["ok"])
        self.assertEqual(tls.ssl_kwargs(), {})   # 没证书就不该启用 TLS

    def test_ssl_kwargs_point_to_real_files(self):
        tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        kw = tls.ssl_kwargs()
        self.assertEqual(set(kw), {"ssl_certfile", "ssl_keyfile"})
        for p in kw.values():
            self.assertTrue(Path(p).is_file())

    def test_key_is_removed_with_cert(self):
        tls.generate([("dns", "localhost"), ("ip", "127.0.0.1")])
        self.assertTrue(tls.remove_cert())
        self.assertFalse(tls.KEY_PATH.is_file())
        self.assertFalse(tls.CERT_PATH.is_file())


if __name__ == "__main__":
    unittest.main()

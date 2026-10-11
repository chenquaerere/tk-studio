"""配置默认值里的每个键都必须真的有人读（回归）。

背景：`common/config.py` 的 DEFAULTS 曾经留着 15 个「死键」——
`app.theme / language / first_run / window`、`collector.default_date /
auto_open_browser_on_start / browser_type / auto_save / task_interval_ms /
error_wait_ms`、`watermark.lan_password / tk_concurrent / tk_retries /
tk_quality / tk_proxy`。它们既没有读取点，界面上也没有控件，却会被写进
用户的 `config.json`，让人误以为「有这个开关」；其中 `lan_password` 更
容易让人以为局域网访问有密码保护（实际没有任何校验）。

这类键靠人眼通读很难发现，所以这里用一个「注释剥离 + 字面量/属性访问」
扫描把它们钉住：DEFAULTS 里每出现一个新键，要么有真实读取点，要么本
用例失败。

判定规则（为什么这么定）：
  * Python/JS 里的键名一定带引号（`"max_retry"`、`'proxy_host'`、
    `.get("notify_card_width")`），所以匹配 `"key"` / `'key'` / `` `key` ``；
  * 前端读配置用的是属性访问（`values.auto_check_update`），所以 JS 里
    额外认 `.key`；
  * 纯按单词边界匹配会误判 —— `browser_type` 在
    `watermark/tiktok/abogus.py` 里是个函数参数名，`language` 是
    `browser_language` 的子串；
  * 必须剥注释，否则一段提到键名的注释就能把死键「救活」；
  * 必须挖掉 `collector_api._COLLECTOR_SETTING_KEYS` 这个白名单块本身 ——
    它只是键名字符串的清单，不是读取点（`task_interval_ms` 就是这样骗过
    了最初的扫描）；
  * 必须排除 `common/config.py` —— DEFAULTS 自身就是键名字符串的来源。
"""
import json
import re
import unittest
from pathlib import Path

from common import config as cconfig
from common import paths

# 已经被删掉的死键：不许换个位置又长回来。
REMOVED_KEYS = {
    "app": ["theme", "language", "first_run", "window"],
    "collector": ["default_date", "auto_open_browser_on_start", "browser_type",
                  "auto_save", "task_interval_ms", "error_wait_ms"],
    "watermark": ["lan_password", "tk_concurrent", "tk_retries",
                  "tk_quality", "tk_proxy"],
}


def _strip_py(text: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return re.sub(r"#[^\n]*", "", text)


def _strip_js(text: str) -> str:
    text = re.sub(r"/\*(?:.|\n)*?\*/", "", text)
    return re.sub(r"//[^\n]*", "", text)


def _corpus():
    """返回 [(标签, 文本, 是否 JS)]：仓库里所有「可能读配置」的源码。"""
    root = Path(paths.APP_ROOT)
    out = []
    py_files = list(root.glob("*.py")) + list((root / "scripts").glob("*.py"))
    py_files += [p for p in (root / "_app").rglob("*.py")
                 if "__pycache__" not in p.parts and "tests" not in p.parts
                 and p.name != "config.py"]
    for p in py_files:
        text = _strip_py(p.read_text(encoding="utf-8", errors="ignore"))
        # 白名单清单本身不是读取点
        text = re.sub(r"_COLLECTOR_SETTING_KEYS\s*=\s*\[(?:.|\n)*?\n\]", "", text)
        out.append((p.name, text, False))
    for p in (root / "_app" / "frontend").rglob("*.js"):
        out.append((p.name, _strip_js(p.read_text(encoding="utf-8", errors="ignore")), True))
    return out


CORPUS = _corpus()


def _readers(key: str):
    quoted = re.compile(r"[\"'`]" + re.escape(key) + r"[\"'`]")
    attr = re.compile(r"\." + re.escape(key) + r"(?![A-Za-z0-9_])")
    return [label for label, text, is_js in CORPUS
            if quoted.search(text) or (is_js and attr.search(text))]


class TestEveryDefaultKeyIsLive(unittest.TestCase):
    def test_corpus_is_not_empty(self):
        """扫描范围本身要有效，否则下面的断言全都是空转。"""
        py = [c for c in CORPUS if not c[2]]
        js = [c for c in CORPUS if c[2]]
        self.assertGreater(len(py), 20, f"Python 扫描面太小：{len(py)}")
        self.assertGreaterEqual(len(js), 2, f"JS 扫描面太小：{len(js)}")

    def test_no_dead_key_in_defaults(self):
        dead = {sec: [k for k in sorted(keys) if not _readers(k)]
                for sec, keys in cconfig.DEFAULTS.items()}
        dead = {sec: ks for sec, ks in dead.items() if ks}
        self.assertEqual(
            dead, {},
            "这些默认值没有任何读取点（要么接上，要么删掉，别留在 config.json 里骗人）")

    def test_removed_keys_stay_removed(self):
        for sec, keys in REMOVED_KEYS.items():
            for key in keys:
                self.assertNotIn(key, cconfig.DEFAULTS.get(sec, {}),
                                 f"{sec}.{key} 又被加回 DEFAULTS 了")

    def test_app_section_only_keeps_last_seen_version(self):
        self.assertEqual(sorted(cconfig.DEFAULTS["app"]), ["last_seen_version"])

    def test_scanner_is_not_vacuous(self):
        """反向验证：活键必须扫得到，编造的键名必须扫不到。"""
        self.assertTrue(_readers("max_retry"), "max_retry 是活键，必须扫到读取点")
        self.assertTrue(_readers("notify_card_width"), "notify_card_width 是活键")
        self.assertEqual(_readers("zzz_not_a_key"), [], "编造的键名不该有命中")

    def test_collector_api_whitelist_subset_of_defaults(self):
        from server import collector_api as ca
        defaults = set(cconfig.DEFAULTS["collector"])
        extra = [k for k in ca._COLLECTOR_SETTING_KEYS if k not in defaults]
        self.assertEqual(extra, [], "白名单暴露了 DEFAULTS 里没有的键")
        for key in ("task_interval_ms", "error_wait_ms"):
            self.assertNotIn(key, ca._COLLECTOR_SETTING_KEYS,
                             f"{key} 没有任何读取点，不该继续暴露在设置接口里")

    def test_example_config_matches_defaults_keys(self):
        """config.example.json 是给用户看的，键集必须和 DEFAULTS 一致。"""
        example_path = Path(paths.APP_ROOT) / "config.example.json"
        example = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(example), sorted(cconfig.DEFAULTS),
                         "示例配置的分区与 DEFAULTS 不一致")
        for sec, defaults in cconfig.DEFAULTS.items():
            self.assertEqual(sorted(example[sec]), sorted(defaults),
                             f"示例配置 [{sec}] 的键与 DEFAULTS 不一致")


if __name__ == "__main__":
    unittest.main()

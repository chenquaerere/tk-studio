"""设置项的「兜底值」必须和配置默认值一致（回归）。

背景：`face_work_side` 在多处各写了一个兜底值 ——
`common/config.py` 的 DEFAULTS 是 4096，水印引擎里却是 `or 2048`，
保存接口里也是 `int(facework or 2048)`。同一项设置出现两个「默认」，
一旦哪条路径真的拿到空值，就会出现「界面显示 4096、实际按 2048 处理」
这种查起来很费劲的偏差（和设置页「存得进、读不回」是同一类问题）。

现在统一以 `common/config.py` 的 DEFAULTS 为唯一来源；本用例扫描源码里
所有 `face_work_side` 的数字兜底，只要与 DEFAULTS 不一致就失败。
"""
import re
import unittest

KEY = "face_work_side"
# 匹配：face_work_side", 4096)  /  facework or 4096  /  face_work_side": 4096
PATTERNS = (
    re.compile(r'face_work_side",\s*(\d+)'),
    re.compile(r"facework\s+or\s+(\d+)"),
    re.compile(r'face_work_side":\s*(\d+)'),
)


class TestFallbackConsistency(unittest.TestCase):
    def test_all_fallbacks_match_defaults(self):
        from common import config as cconfig
        from common import paths

        want = str(cconfig.DEFAULTS["hd"][KEY])
        self.assertEqual(want, "4096", "默认值变了就要同步各处兜底")

        app = paths.APP_ROOT / "_app"
        found = {}
        for path in app.rglob("*.py"):
            if "/tests/" in path.as_posix() or path.name == "config.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if KEY not in text and "facework" not in text:
                continue
            for pattern in PATTERNS:
                for value in pattern.findall(text):
                    found.setdefault(str(path.relative_to(app)), set()).add(value)

        self.assertTrue(found, "一个兜底值都没扫到，说明扫描逻辑失效了")
        for path, values in found.items():
            self.assertEqual(values, {want},
                             f"{path} 里 {KEY} 的兜底值 {values} 与默认值 {want} 不一致")


if __name__ == "__main__":
    unittest.main()

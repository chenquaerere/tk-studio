"""前端转义必须连引号一起处理：昵称/地区里带引号不能把表单撑破。

缺陷（2026-10-10 复盘）：app.js 里的 `esc()` 只转义了 `& < >`，
但它同时被用在 HTML 属性位置（`value="${esc(...)}"`）。账号昵称、地区、备注
都可能带一个 `"`（TikTok 昵称允许引号，用户也能手输），于是：

    <input value="小"明 autofocus onfocus=...">

属性被提前闭合 —— 轻则表单显示不全/内容被截断，重则注入属性执行脚本
（这是 pywebview 里能直接调本地接口的前端，属性注入不是纯理论问题）。

这里用 node 真跑一遍 app.js 里的 `esc`（node 不在 PATH 时自动跳过），
同时也断言「老写法确实会被引号撑破」，避免测试变成空转。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[2]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

_JS_DIR = _APP_DIR / "frontend" / "js"
_APP_JS = _JS_DIR / "app.js"
_COLLECTOR_JS = _JS_DIR / "collector.js"

# esc 是一串 `.replace(...)` 链，末尾是 `&#39;");`
_ESC_RE = re.compile(r"const esc = [\s\S]*?&#39;\"\);")

NODE = shutil.which("node")


def _esc_statement() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    m = _ESC_RE.search(src)
    if not m:
        raise AssertionError("app.js 里找不到 esc 定义（测试需要更新）")
    return m.group(0)


def _strip_comments(src: str) -> str:
    """去掉注释再扫描：注释里写 `value="${...}"` 只是说明，不是可执行插值。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _attribute_interpolations(src: str) -> list[str]:
    """取出 `...="${...}"`（HTML 属性位置）里的插值表达式。"""
    out = []
    for m in re.finditer(r'="\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"', _strip_comments(src)):
        out.append(m.group(1).strip())
    return out


class TestEscapeSource(unittest.TestCase):
    def test_esc_escapes_quotes_too(self) -> None:
        stmt = _esc_statement()
        self.assertIn('.replace(/"/g, "&quot;")', stmt)
        self.assertIn(".replace(/'/g, \"&#39;\")", stmt)
        self.assertIn('.replace(/&/g, "&amp;")', stmt)

    def test_attribute_interpolations_are_escaped_or_numeric(self) -> None:
        """属性位置的插值要么 esc()，要么是数字/字面量。"""
        offenders = []
        for path in (_APP_JS, _COLLECTOR_JS):
            for expr in _attribute_interpolations(path.read_text(encoding="utf-8")):
                if "esc(" in expr:
                    continue
                # 允许：纯数字取值（counts[0]）、空串、布尔三元
                if re.fullmatch(r'[A-Za-z_$][\w$.\[\]?]*|""|\'\'', expr):
                    continue
                if "counts[0]" in expr:
                    continue
                offenders.append((path.name, expr))
        self.assertEqual(offenders, [], f"属性位置有未转义插值：{offenders}")


@unittest.skipUnless(NODE, "node 不在 PATH，跳过行为验证")
class TestEscapeBehaviour(unittest.TestCase):
    PAYLOAD = '小"明 onfocus=alert(1) \'x\' <b>&'

    def _run(self, body: str) -> str:
        script = _esc_statement() + "\n" + body
        proc = subprocess.run([NODE, "-e", script], capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_quotes_do_not_break_out_of_attribute(self) -> None:
        out = self._run(
            "const p = %s;\n"
            "process.stdout.write(esc(p));" % json.dumps(self.PAYLOAD))
        self.assertNotIn('"', out)
        self.assertNotIn("'", out)
        self.assertIn("&quot;", out)
        self.assertIn("&#39;", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&amp;", out)

    def test_old_three_char_escape_would_break(self) -> None:
        """反向对照：只转义 & < > 的老写法确实会被引号撑破。"""
        out = self._run(
            "const old = (v) => String(v == null ? '' : v)"
            ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
            "process.stdout.write('<input value=\"' + old(%s) + '\">');"
            % json.dumps(self.PAYLOAD))
        self.assertIn('value="小"', out)          # 属性在这里被提前闭合
        self.assertIn("onfocus=alert(1)", out)    # 注入的内容跑到了属性外面

    def test_nickname_round_trips_in_input_value(self) -> None:
        """带引号的昵称放进 input value 后，取回来还是原值。"""
        out = self._run(
            "const p = %s;\n"
            "const html = '<input value=\"' + esc(p) + '\">';\n"
            "const m = html.match(/value=\"([^\"]*)\"/);\n"
            "const decoded = m[1].replace(/&quot;/g, '\"').replace(/&#39;/g, \"'\")\n"
            "  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');\n"
            "process.stdout.write(decoded);" % json.dumps(self.PAYLOAD))
        self.assertEqual(out, self.PAYLOAD)


if __name__ == "__main__":
    unittest.main()

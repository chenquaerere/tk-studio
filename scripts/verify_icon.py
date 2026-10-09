"""校验 exe 内嵌图标与 assets/icon.ico 一致（纯 stdlib，不依赖任何 PE 库）。

原理
----
ICO 文件结构 = ICONDIR(6B) + N × ICONDIRENTRY(16B) + 各帧图像数据。
`<ApplicationIcon>` / `/win32icon` 编译时，会把每一帧的**图像数据原样**
写进 PE 的 RT_ICON 资源。

所以只要把 icon.ico 里各帧的字节序列拿到，再去 exe 的字节流里搜，
命中即证明「exe 内嵌的图标就是这份 ico」。

用法：
    python scripts/verify_icon.py [exe路径] [ico路径]
默认：
    exe = <程序根>/TK素材工坊.exe
    ico = <程序根>/assets/icon.ico
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_ico(ico_path: Path) -> list[tuple[int, int, bytes]]:
    """返回 [(宽, 高, 帧数据)]，按 ICO 目录顺序。"""
    raw = ico_path.read_bytes()
    if len(raw) < 6:
        raise ValueError("不是合法的 ICO：文件太短")
    reserved, type_, count = struct.unpack_from("<HHH", raw, 0)
    if reserved != 0 or type_ != 1:
        raise ValueError(f"不是合法的 ICO：reserved={reserved} type={type_}")
    frames = []
    for i in range(count):
        off = 6 + i * 16
        w, h, _colors, _res, _planes, _bpp, size, data_off = struct.unpack_from(
            "<BBBBHHII", raw, off)
        w = w or 256
        h = h or 256
        frames.append((w, h, raw[data_off:data_off + size]))
    return frames


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "TK素材工坊.exe"
    ico = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "assets" / "icon.ico"

    print(f"exe : {exe}")
    print(f"ico : {ico}")
    if not exe.exists():
        print("[FAIL] exe 不存在")
        return 1
    if not ico.exists():
        print("[FAIL] ico 不存在")
        return 1

    blob = exe.read_bytes()
    frames = parse_ico(ico)
    print(f"\nicon.ico 共 {len(frames)} 帧：")

    hits = 0
    for w, h, data in frames:
        is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
        found = blob.find(data) >= 0 if len(data) > 64 else None
        tag = "PNG" if is_png else "BMP"
        if found:
            hits += 1
        mark = "命中" if found else ("跳过(帧太小)" if found is None else "未命中")
        print(f"  {w:>3}x{h:<3} {tag}  {len(data):>8} B   {mark}")

    print()
    if hits:
        print(f"[PASS] exe 内嵌了 {hits}/{len(frames)} 帧原始图标数据 —— "
              f"与 assets/icon.ico 同源")
        print(f"       exe 体积 {len(blob) / 1024:.1f} KB")
        return 0
    print("[FAIL] exe 内未找到任何一帧图标数据")
    return 1


if __name__ == "__main__":
    sys.exit(main())

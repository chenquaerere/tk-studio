"""生成程序图标资源（方案 A · 棱镜）。

设计概念
--------
红光射入、青光射出，寓意「模糊进、清晰出」，同时承载 TikTok 红与抖音青双色品牌。

输出
----
assets/icon.ico    16 / 24 / 32 / 48 / 64 / 128 / 256 七种尺寸
assets/icon.png    512×512 主图
assets/favicon.ico 32 / 48（浏览器标签页）

大小尺寸采用两种画法
--------------------
≥48px：三角棱镜描边 + 红青光束（完整设计）
≤32px：实心三角，左半红右半青（简化设计，避免细线在小尺寸下消失）

用法：python scripts/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"

BG_TOP = (30, 30, 42, 255)
BG_BOTTOM = (16, 16, 22, 255)
RED = (254, 44, 85, 255)
CYAN = (37, 244, 238, 255)
OUTLINE = (156, 163, 175, 255)
GLASS = (255, 255, 255, 16)

SS = 4  # 超采样倍数


def _rounded_bg(size: int, gradient: bool = True) -> Image.Image:
    """圆角方形底：带轻微纵向渐变，避免纯平的廉价感。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255
    )
    if gradient:
        grad = Image.new("RGBA", (1, size))
        for y in range(size):
            t = y / max(1, size - 1)
            grad.putpixel((0, y), tuple(
                int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(4)
            ))
        img = grad.resize((size, size))
    else:
        img = Image.new("RGBA", (size, size), BG_TOP)
    img.putalpha(mask)
    return img


def _tri_points(size: int) -> list[tuple[float, float]]:
    m = size * 0.215
    return [((size / 2), m), (m, size - m), (size - m, size - m)]


def draw_large(size: int) -> Image.Image:
    """完整设计：棱镜描边 + 红/青光束（用于 ≥48px）。"""
    S = size * SS
    base = _rounded_bg(S)
    d = ImageDraw.Draw(base, "RGBA")

    apex, bl, br = _tri_points(S)

    # 光在棱镜内的折射路径（先画，让三角玻璃盖在上面）
    bw = S * 0.092                      # 光束粗细
    y_in = S * 0.615                    # 入射高度
    y_out = S * 0.520                   # 出射高度

    def edge_x(p1, p2, y):
        (x1, y1), (x2, y2) = p1, p2
        if y2 == y1:
            return x1
        return x1 + (x2 - x1) * (y - y1) / (y2 - y1)

    x_hit = edge_x(apex, bl, y_in)

    # 入射红光
    d.line([(S * 0.055, y_in), (x_hit + bw * 0.35, y_in)],
           fill=RED, width=int(bw), joint="curve")
    # 出射青光
    x_leave = edge_x(apex, br, y_out)
    d.line([(x_leave - bw * 0.35, y_out), (S * 0.945, y_out)],
           fill=CYAN, width=int(bw), joint="curve")

    # 棱镜玻璃体
    d.polygon([apex, bl, br], fill=GLASS)
    d.line([apex, bl, br, apex], fill=OUTLINE, width=max(2, int(S * 0.016)),
           joint="curve")

    # 顶部高光，增强「玻璃」质感
    hl = [(apex[0], apex[1] + S * 0.03),
          (apex[0] - (apex[0] - bl[0]) * 0.34, apex[1] + (bl[1] - apex[1]) * 0.34)]
    d.line(hl, fill=(255, 255, 255, 90), width=max(2, int(S * 0.011)))

    return base.resize((size, size), Image.LANCZOS)


def draw_small(size: int) -> Image.Image:
    """简化设计：实心三角，左半红右半青（用于 ≤32px，细线会消失故不用描边）。"""
    S = size * SS
    base = _rounded_bg(S)

    tri = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tri).polygon(_tri_points(S), fill=255)

    left = tri.copy()
    ImageDraw.Draw(left).rectangle([S // 2, 0, S, S], fill=0)

    base.paste(CYAN, (0, 0), tri)
    base.paste(RED, (0, 0), left)

    return base.resize((size, size), Image.LANCZOS)


def build(size: int) -> Image.Image:
    return draw_large(size) if size >= 48 else draw_small(size)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = {s: build(s) for s in ico_sizes}

    frames[256].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=[frames[s] for s in ico_sizes if s != 256],
    )
    print("icon.ico   ->", ", ".join(f"{s}px" for s in ico_sizes))

    build(512).save(ASSETS / "icon.png")
    print("icon.png   -> 512px")

    fav = [32, 48]
    frames[48].save(
        ASSETS / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s in fav],
        append_images=[frames[32]],
    )
    print("favicon.ico->", ", ".join(f"{s}px" for s in fav))

    # PNG 快照，便于外部预览
    for s in (32, 64, 256):
        frames[s].save(ASSETS / f"icon_{s}.png")
    print("预览图     -> icon_32/64/256.png")


if __name__ == "__main__":
    main()

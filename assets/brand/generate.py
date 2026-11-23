#!/usr/bin/env python3
"""wowsDG shield-anchor brand mark generator.

Geometry measured from the reference JPEG (447x447): 0.1px precision,
fully symmetric about x=223.15. Palette: yellow #FFC400 / black #0C0C0C.

    py -3 generate.py

Set WOWS_ICON_REFERENCE to a reference image to enable the optional IoU
overlay and the "reference" tile in preview.png; without it those are skipped.

Outputs: mark-*.svg, icon-*.svg, png/*.png, wows-icon.ico, preview.png
"""
from __future__ import annotations

import io
import os
import struct

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PNG_DIR = os.path.join(HERE, "png")
os.makedirs(PNG_DIR, exist_ok=True)

# Optional reference art for the QA overlay/contact sheet. Point at your own
# copy with WOWS_ICON_REFERENCE; no reference image is distributed.
REFERENCE = os.environ.get(
    "WOWS_ICON_REFERENCE", os.path.join(HERE, "reference.jpg"))

YELLOW = "#FFC400"
BLACK = "#0C0C0C"
YELLOW_RGB = (255, 196, 0)
BLACK_RGB = (12, 12, 12)

# ---------------------------------------------------------------- geometry
CX = 223.15  # symmetry axis in reference pixel space

# Shield ring: 18.8px uniform border, straight sides, chevron bottom.
SHIELD_OUTER = [
    (CX - 111.85, 94.2), (CX + 111.85, 94.2), (CX + 111.85, 320.8),
    (CX, 366.8), (CX - 111.85, 320.8),
]
SHIELD_INNER = [
    (CX - 93.05, 113.0), (CX + 93.05, 113.0), (CX + 93.05, 308.0),
    (CX, 346.5), (CX - 93.05, 308.0),
]
# Anchor: top block -> shaft -> crossbar -> shaft -> bowl with flukes.
ANCHOR = [
    (CX - 36.4, 131.9),   # top block, top-left
    (CX - 36.4, 157.3),   # block side down
    (CX - 18.4, 168.9),   # chamfer into shaft
    (CX - 18.4, 178.2),   # shaft down to crossbar
    (CX - 55.25, 178.2),  # crossbar top edge
    (CX - 55.25, 204.5),  # crossbar end
    (CX - 27.0, 214.4),   # underside chamfer
    (CX - 18.4, 216.0),   # small shelf into shaft
    (CX - 18.4, 279.5),   # shaft down to slot floor
    (CX - 37.4, 270.5),   # fluke flare
    (CX - 37.4, 234.0),   # fluke inner edge
    (CX - 74.0, 217.4),   # fluke tip
    (CX - 74.0, 295.6),   # fluke outer edge down
    (CX, 326.0),          # bowl chevron tip
    (CX + 74.0, 295.6),
    (CX + 74.0, 217.4),
    (CX + 37.4, 234.0),
    (CX + 37.4, 270.5),
    (CX + 18.4, 279.5),
    (CX + 18.4, 216.0),
    (CX + 27.0, 214.4),
    (CX + 55.25, 204.5),
    (CX + 55.25, 178.2),
    (CX + 18.4, 178.2),
    (CX + 18.4, 168.9),
    (CX + 36.4, 157.3),
    (CX + 36.4, 131.9),
]
MARK_BBOX = (CX - 111.85, 94.2, CX + 111.85, 366.8)  # x0,y0,x1,y1
MARK_W = MARK_BBOX[2] - MARK_BBOX[0]
MARK_H = MARK_BBOX[3] - MARK_BBOX[1]
MARK_CX = (MARK_BBOX[0] + MARK_BBOX[2]) / 2
MARK_CY = (MARK_BBOX[1] + MARK_BBOX[3]) / 2

MARK_RATIO = 0.66         # mark height / square icon side (app icon)
TRANSPARENT_RATIO = 0.82  # mark height / canvas side for standalone PNGs
SQ_RADIUS = 0.225         # rounded-square corner radius ratio

# ---------------------------------------------------------------- SVG writer
def fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def path_d(points) -> str:
    return "M " + " L ".join(f"{fmt(x)} {fmt(y)}" for x, y in points) + " Z"


def shield_path_d() -> str:
    # outer + inner in one path, even-odd => ring with hole
    return path_d(SHIELD_OUTER) + " " + path_d(SHIELD_INNER)


def mark_paths(fill: str) -> str:
    return (
        f'  <path fill="{fill}" fill-rule="evenodd" d="{shield_path_d()}"/>\n'
        f'  <path fill="{fill}" d="{path_d(ANCHOR)}"/>\n'
    )


def mark_svg(fill: str, title: str) -> str:
    pad = 10.0
    vb = (MARK_BBOX[0] - pad, MARK_BBOX[1] - pad,
          MARK_W + 2 * pad, MARK_H + 2 * pad)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="'
        f'{fmt(vb[0])} {fmt(vb[1])} {fmt(vb[2])} {fmt(vb[3])}" role="img" aria-label="{title}">\n'
        f'  <title>{title}</title>\n'
        + mark_paths(fill)
        + "</svg>\n"
    )


def square_icon_svg(bg: str, fg: str, title: str) -> str:
    n = 1024.0
    k = MARK_RATIO * n / MARK_H
    tx = n / 2 - k * MARK_CX
    ty = n / 2 - k * MARK_CY
    r = SQ_RADIUS * n
    body = mark_paths(fg).replace("\n", "\n    ")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" '
        f'aria-label="{title}">\n'
        f'  <title>{title}</title>\n'
        f'  <rect width="1024" height="1024" rx="{fmt(r)}" fill="{bg}"/>\n'
        f'  <g transform="translate({fmt(tx)} {fmt(ty)}) scale({k:.6f})">\n'
        f'    {body.rstrip()}\n'
        '  </g>\n'
        '</svg>\n'
    )

# ---------------------------------------------------------------- raster
def render_mark_mask(size: int, ratio: float, supersample: int = 4) -> Image.Image:
    """Anti-aliased mask (L) of the mark, mark height = ratio*size."""
    ss = size * supersample
    k = ratio * ss / MARK_H
    tx, ty = ss / 2 - k * MARK_CX, ss / 2 - k * MARK_CY

    def T(p):
        return (p[0] * k + tx, p[1] * k + ty)

    im = Image.new("L", (ss, ss), 0)
    d = ImageDraw.Draw(im)
    d.polygon([T(p) for p in SHIELD_OUTER], fill=255)
    d.polygon([T(p) for p in SHIELD_INNER], fill=0)
    d.polygon([T(p) for p in ANCHOR], fill=255)
    return im.resize((size, size), Image.LANCZOS)


def tint(mask: Image.Image, rgb) -> Image.Image:
    out = Image.new("RGBA", mask.size, rgb + (0,))
    out.putalpha(mask)
    return out


def rounded_mask(size: int, radius_ratio: float, supersample: int = 4) -> Image.Image:
    ss = size * supersample
    im = Image.new("L", (ss, ss), 0)
    d = ImageDraw.Draw(im)
    r = radius_ratio * ss
    d.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=r, fill=255)
    return im.resize((size, size), Image.LANCZOS)


def square_icon(size: int, bg_rgb, fg_rgb, rounded: bool = True,
                mark_ratio: float = MARK_RATIO) -> Image.Image:
    bg = Image.new("RGBA", (size, size), bg_rgb + (0,))
    if rounded:
        bg.putalpha(rounded_mask(size, SQ_RADIUS))
    else:
        bg.putalpha(255)
    mark = tint(render_mark_mask(size, mark_ratio), fg_rgb)
    bg.alpha_composite(mark)
    return bg


def save_png(im: Image.Image, name: str):
    path = os.path.join(PNG_DIR, name)
    im.save(path, "PNG", optimize=True)
    return path

# ---------------------------------------------------------------- ICO writer
def write_ico(images, path):
    """images: list of (size, PIL RGBA). PNG-compressed entries (Vista+)."""
    images = sorted(images, key=lambda t: t[0])
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs, offset = b"", b"", 6 + 16 * len(images)
    for size, im in images:
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        blob = buf.getvalue()
        w = size if size < 256 else 0
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        blobs += blob
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)

# ---------------------------------------------------------------- QA overlay
def qa_overlay(src_path: str, out_path: str) -> float:
    """Overlay reconstructed mark on the reference image; returns IoU."""
    import numpy as np
    ref = Image.open(src_path).convert("RGB")
    a = np.asarray(ref).astype(np.int16)
    ref_mask = (a.min(axis=2) > 150) & (a.max(axis=2) - a.min(axis=2) < 60)

    ss = 4
    im = Image.new("L", (ref.width * ss, ref.height * ss), 0)
    d = ImageDraw.Draw(im)
    d.polygon([(x * ss, y * ss) for x, y in SHIELD_OUTER], fill=255)
    d.polygon([(x * ss, y * ss) for x, y in SHIELD_INNER], fill=0)
    d.polygon([(x * ss, y * ss) for x, y in ANCHOR], fill=255)
    mask = im.resize(ref.size, Image.LANCZOS)
    m = np.asarray(mask) > 127
    iou = (m & ref_mask).sum() / (m | ref_mask).sum()

    base = ref.convert("RGBA")
    overlay = Image.new("RGBA", ref.size, (255, 0, 255, 0))
    overlay.putalpha(mask.point(lambda v: int(v * 0.55)))
    base.alpha_composite(overlay)
    base.convert("RGB").save(out_path)
    return float(iou)

# ---------------------------------------------------------------- build
def main():
    # standalone marks (transparent)
    with open(os.path.join(HERE, "mark-yellow.svg"), "w", encoding="utf-8") as f:
        f.write(mark_svg(YELLOW, "wowsDG shield-anchor mark"))
    with open(os.path.join(HERE, "mark-black.svg"), "w", encoding="utf-8") as f:
        f.write(mark_svg(BLACK, "wowsDG shield-anchor mark"))
    # square app icons
    with open(os.path.join(HERE, "icon-black-yellow.svg"), "w", encoding="utf-8") as f:
        f.write(square_icon_svg(BLACK, YELLOW, "wowsDG icon, yellow on black"))
    with open(os.path.join(HERE, "icon-yellow-black.svg"), "w", encoding="utf-8") as f:
        f.write(square_icon_svg(YELLOW, BLACK, "wowsDG icon, black on yellow"))

    # transparent standalone PNGs
    for size in (1024, 512, 256):
        save_png(tint(render_mark_mask(size, TRANSPARENT_RATIO, 4), YELLOW_RGB),
                 f"mark-yellow-{size}.png")
        save_png(tint(render_mark_mask(size, TRANSPARENT_RATIO, 4), BLACK_RGB),
                 f"mark-black-{size}.png")

    # square icon PNGs
    for size in (1024, 512, 256):
        save_png(square_icon(size, BLACK_RGB, YELLOW_RGB), f"icon-black-yellow-{size}.png")
        save_png(square_icon(size, YELLOW_RGB, BLACK_RGB), f"icon-yellow-black-{size}.png")
    # full-bleed square (matches the original composition)
    save_png(square_icon(1024, BLACK_RGB, YELLOW_RGB, rounded=False),
             "icon-black-yellow-square-1024.png")
    save_png(square_icon(1024, YELLOW_RGB, BLACK_RGB, rounded=False),
             "icon-yellow-black-square-1024.png")

    # multi-size Windows icon
    ico = []
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ico.append((size, square_icon(size, BLACK_RGB, YELLOW_RGB, mark_ratio=0.70)))
    write_ico(ico, os.path.join(HERE, "wows-icon.ico"))

    # QA vs source (written outside the deliverable folder)
    reference = load_reference()
    if reference is not None:
        import tempfile
        qa_path = os.path.join(tempfile.gettempdir(), "wows_icon_qa_overlay.png")
        iou = qa_overlay(REFERENCE, qa_path)
        print(f"geometry IoU vs reference: {iou:.3f}")
    else:
        print("reference image not found; skipping IoU "
              "(set WOWS_ICON_REFERENCE=<path> to enable)")

    # contact sheet
    make_preview(reference)


def load_reference():
    if REFERENCE and os.path.isfile(REFERENCE):
        return Image.open(REFERENCE).convert("RGB")
    return None


def make_preview(reference=None):
    tiles = [
        ("原图（参考）", "orig"),
        ("独立标 · 黄（深底）", "mark_dark"),
        ("独立标 · 黑（浅底）", "mark_light"),
        ("黑底黄标 · 图标", "icon_by"),
        ("黄底黑标 · 图标", "icon_yb"),
        ("黑底黄标 · 方形", "square_by"),
        ("黄底黑标 · 方形", "square_yb"),
        ("尺寸阶梯 256 / 64 / 32 / 16", "ladder"),
    ]
    cols, tw, th, gap, top = 4, 360, 360, 48, 92
    W = cols * tw + (cols + 1) * gap
    H = top + 2 * (th + 76) + 40
    sheet = Image.new("RGB", (W, H), (26, 26, 26))
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 30)
        small = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 20)
    except OSError:
        font = small = ImageFont.load_default()

    if reference is not None:
        orig = reference.resize((tw, th), Image.LANCZOS)
    else:
        orig = Image.new("RGB", (tw, th), (40, 40, 40))
        ImageDraw.Draw(orig).text(
            (tw // 2, th // 2), "reference image not provided",
            anchor="mm", font=small, fill=(150, 150, 150))
    yellow = tint(render_mark_mask(tw, TRANSPARENT_RATIO, 4), YELLOW_RGB)
    black = tint(render_mark_mask(tw, TRANSPARENT_RATIO, 4), BLACK_RGB)
    icon_by = square_icon(tw, BLACK_RGB, YELLOW_RGB)
    icon_yb = square_icon(tw, YELLOW_RGB, BLACK_RGB)
    sq_by = square_icon(tw, BLACK_RGB, YELLOW_RGB, rounded=False)
    sq_yb = square_icon(tw, YELLOW_RGB, BLACK_RGB, rounded=False)

    def on_bg(im, bg):
        canvas = Image.new("RGB", (tw, th), bg)
        canvas.paste(im, (0, 0), im)
        return canvas

    ladder = Image.new("RGB", (tw, th), (20, 20, 20))
    ld = ImageDraw.Draw(ladder)
    big = square_icon(256, BLACK_RGB, YELLOW_RGB, mark_ratio=0.70)
    ladder.paste(big, (16, 52), big)
    y = 40
    for sz in (64, 32, 16):
        im = square_icon(sz, BLACK_RGB, YELLOW_RGB, mark_ratio=0.70)
        x = 288
        ladder.paste(im, (x, y), im)
        ld.text((x, y + sz + 6), f"{sz}px", font=small, fill=(210, 210, 210))
        y += sz + 52

    contents = {
        "orig": orig,
        "mark_dark": on_bg(yellow, (20, 20, 20)),
        "mark_light": on_bg(black, (243, 243, 243)),
        "icon_by": icon_by.convert("RGB"),
        "icon_yb": icon_yb.convert("RGB"),
        "square_by": sq_by.convert("RGB"),
        "square_yb": sq_yb.convert("RGB"),
        "ladder": ladder,
    }

    d = ImageDraw.Draw(sheet)
    for i, (label, key) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = gap + c * (tw + gap)
        y = top + r * (th + 76)
        sheet.paste(contents[key], (x, y))
        d.text((x, y + th + 16), label, font=font, fill=(230, 230, 230))
    sheet.save(os.path.join(HERE, "preview.png"), "PNG", optimize=True)


if __name__ == "__main__":
    main()
    print("wrote:", HERE)

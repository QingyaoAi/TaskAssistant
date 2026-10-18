#!/usr/bin/python3
"""把 TaskAssistant.jpeg 做成 macOS 图标（AppIcon.icns）。

原图是白底上的一个圆角方块（带阴影），方块只占画面中间约一半。这里把方块裁出来、四角做透明，
按 macOS 图标规范缩到 1024 画布里的 824×824 居中，加一点柔和投影，再用 iconutil 生成 icns。
    /usr/bin/python3 jobs/launcher/make_icon.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / 'TaskAssistant.jpeg'
OUT = HERE / 'AppIcon.icns'
# 原图里圆角方块的位置和圆角半径（2048×2048 原图上实测）。
BOX = (512, 511, 1536, 1535)
RADIUS = 230
CANVAS, ART = 1024, 824


def rounded_mask(size: int, radius: int, scale: int = 4) -> Image.Image:
    big = Image.new('L', (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, size * scale - 1, size * scale - 1), radius * scale, fill=255)
    return big.resize((size, size), Image.LANCZOS)


def master() -> Image.Image:
    src = Image.open(SOURCE).convert('RGB').crop(BOX)
    side = src.size[0]
    art = src.convert('RGBA')
    art.putalpha(rounded_mask(side, RADIUS))
    art = art.resize((ART, ART), Image.LANCZOS)
    canvas = Image.new('RGBA', (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - ART) // 2
    shadow = Image.new('RGBA', (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 70), (offset, offset + 10, offset + ART, offset + ART + 10), rounded_mask(ART, RADIUS * ART // side))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(14)))
    canvas.alpha_composite(art, (offset, offset))
    return canvas


def main() -> int:
    icon = master()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / 'AppIcon.iconset'
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            icon.resize((size, size), Image.LANCZOS).save(iconset / f'icon_{size}x{size}.png')
            icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f'icon_{size}x{size}@2x.png')
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(OUT)], check=True)
    icon.save(HERE / 'AppIcon-preview.png')
    print('已生成：', OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())

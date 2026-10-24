"""用用户给的成品 PNG 生成各尺寸图标（不做任何重绘，只做等比缩放）

用法：python tools/make_icon_png.py <图片路径>
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    ROOT / "assets" / "logo.png": 512,
    ROOT / "server" / "static" / "logo.png": 512,
    ROOT / "server" / "static" / "favicon.png": 64,
}

src = Image.open(sys.argv[1]).convert("RGBA")
# 非正方形时补透明边，保证缩放后不变形
if src.width != src.height:
    side = max(src.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(src, ((side - src.width) // 2, (side - src.height) // 2))
    src = canvas

for path, size in TARGETS.items():
    src.resize((size, size), Image.LANCZOS).save(path)
    print(f"{path.name}: {size}x{size}")

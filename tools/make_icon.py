"""把 assets/icon-src/*.svg 渲染成 PNG，并拼一张候选对比图。

用法：python tools/make_icon.py            # 渲染 icon.svg 到各处
      python tools/make_icon.py --preview  # 额外输出候选对比图
"""
import base64
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "icon-src" / "icon.svg"

CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
          r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]


def _shot(html: str, w: int, h: int) -> bytes:
    """用本机浏览器渲染 HTML 截图（无第三方依赖）"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "i.html"
        p.write_text(html, encoding="utf-8")
        out = Path(d) / "i.png"
        for exe in CHROME:
            if not Path(exe).exists():
                continue
            subprocess.run([exe, "--headless", "--disable-gpu", "--hide-scrollbars",
                            f"--screenshot={out}", f"--window-size={w},{h}",
                            "--default-background-color=00000000", str(p)],
                           check=True, capture_output=True, timeout=120)
            if out.exists():
                return out.read_bytes()
    raise SystemExit("没有找到可用的 Chrome/Edge")


def _from_chrome(svg: bytes, size: int) -> bytes:
    b64 = base64.b64encode(svg).decode()
    html = ('<html><body style="margin:0">'
            f'<img src="data:image/svg+xml;base64,{b64}" width="{size}" height="{size}">'
            "</body></html>")
    return _shot(html, size, size)


def preview() -> None:
    files = sorted((ROOT / "assets" / "icon-src").glob("cand-*.svg"))
    if not files:
        return
    cards = []
    for f in files:
        b64 = base64.b64encode(f.read_bytes()).decode()
        cards.append(
            '<div style="text-align:center;font:15px sans-serif;color:#8a94a6">'
            f'<img src="data:image/svg+xml;base64,{b64}" width="176" height="176">'
            f'<div>{f.stem}</div></div>')
    html = ('<html><body style="margin:0;display:flex;gap:26px;padding:26px;'
            'background:#0d1018">' + "".join(cards) + "</body></html>")
    out = ROOT / "assets" / "icon-src" / "preview.png"
    out.write_bytes(_shot(html, 26 * 2 + 202 * len(cards), 250))
    print(f"{out}  候选对比图")


def main() -> None:
    if "--preview" in sys.argv:
        preview()
        return
    svg = SRC.read_bytes()
    targets = [
        (ROOT / "assets" / "logo.png", 512),
        (ROOT / "server" / "static" / "logo.png", 512),
        (ROOT / "server" / "static" / "favicon.png", 64),
    ]
    for path, size in targets:
        path.write_bytes(_from_chrome(svg, size))
        print(f"{path}  {size}x{size}")


if __name__ == "__main__":
    main()

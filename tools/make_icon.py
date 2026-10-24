"""把 assets/icon-src/icon.svg 渲染成 PNG 图标。

用法：python tools/make_icon.py
输出：assets/logo.png、server/static/logo.png、server/static/favicon.png
"""
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "icon-src" / "icon.svg"


def _from_chrome(svg: bytes, size: int) -> bytes:
    """用本机浏览器渲染 SVG（无第三方依赖）"""
    import subprocess
    import tempfile

    html = (
        '<html><body style="margin:0">'
        f'<img id="i" src="data:image/svg+xml;base64,{base64.b64encode(svg).decode()}" '
        f'width="{size}" height="{size}">'
        "</body></html>"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "i.html"
        p.write_text(html, encoding="utf-8")
        out = Path(d) / "i.png"
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if not Path(exe).exists():
                continue
            subprocess.run([exe, "--headless", "--disable-gpu", "--hide-scrollbars",
                            f"--screenshot={out}", f"--window-size={size},{size}",
                            "--default-background-color=00000000", str(p)],
                           check=True, capture_output=True, timeout=120)
            if out.exists():
                return out.read_bytes()
    raise SystemExit("没有找到可用的 Chrome/Edge")


def main() -> None:
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

#!/usr/bin/env python3
"""DeckMeido mDNS 发布：让局域网设备直接用 deckmeido.local 访问（不依赖 avahi，用户态 zeroconf）"""
import os
import socket
import time

from zeroconf import ServiceInfo, Zeroconf

PORT = int(os.environ.get("DECKMEIDO_PORT", "18765"))
HOST = "deckmeido.local"


def lan_ip() -> str:
    """取真正的局域网地址。

    不能只看「默认路由出口」：开了 Clash 之类的代理后，出口会是 Meta/TUN 的
    198.18.x.x 虚拟地址，别的设备根本连不上。所以优先挑私有网段里的物理网卡。
    """
    try:
        import psutil
    except ImportError:
        psutil = None

    candidates = []
    if psutil:
        for name, addrs in psutil.net_if_addrs().items():
            low = name.lower()
            # 跳过虚拟网卡（代理 / 容器 / 虚拟机）
            if any(k in low for k in ("meta", "tun", "tap", "docker", "veth",
                                       "br-", "virbr", "vmnet", "wg", "zt")):
                continue
            for a in addrs:
                if a.family != socket.AF_INET or not a.address:
                    continue
                ip = a.address
                if ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.",
                                  "172.19.", "172.2", "172.30.", "172.31.")):
                    candidates.append((0 if low.startswith(("wlan", "wlp", "wl")) else 1, ip))

    if candidates:
        candidates.sort()
        return candidates[0][1]

    # 兜底：走一次外部 UDP 看出口地址
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


ip = lan_ip()
zc = Zeroconf()
info = ServiceInfo(
    "_http._tcp.local.",
    "DeckMeido._http._tcp.local.",
    addresses=[socket.inet_aton(ip)],
    port=PORT,
    server=f"{HOST}.",
)
zc.register_service(info)
print(f"{HOST} -> {ip}", flush=True)
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    zc.unregister_service(info)
    zc.close()

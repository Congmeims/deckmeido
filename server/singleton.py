"""后端单实例保护

一份后端独占一个端口。历史上出现过两份后端抢同一个 18765 的情况：
安装脚本装了一个开机自启的 systemd 服务，客户端启动时又自己拉一份，
谁先绑定端口谁对外服务，而两者各自的内存历史互相独立——
重启时被另一个人抢到，用户看到的就是「聊天记录凭空消失」（磁盘上其实还在）。

这里在监听端口之前做一次「端口是否已被占」的检查，第二个后端直接退出，
让「谁在服务」永远只有一个答案。
"""
from __future__ import annotations

import errno
import socket
import sys

# 允许第二个实例接管：自检脚本/重启流程里偶尔需要临时探测一下
ENV_BYPASS = "DECKMEIDO_ALLOW_SECOND_BACKEND"


def already_serving(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """端口上是否已经有服务在应答。

    只做 TCP 握手，不发 HTTP：绑定失败但进程还没 accept 完时也能判断出来。
    """
    for probe_host in (host, "0.0.0.0"):
        try:
            with socket.create_connection((probe_host, port), timeout=timeout):
                return True
        except OSError as exc:
            # 连接被拒绝 = 端口空着；地址不可用同样是空着
            if exc.errno in (errno.ECONNREFUSED, errno.EADDRNOTAVAIL):
                continue
            # 超时等其它情况：可能刚好有人在 accept，保守认为有服务
            return True
    return False


def guard(port: int, allow_second: bool = False) -> None:
    """启动前调用：端口已被占就直接结束本进程。

    uvicorn 自己绑定失败也会退出，但会先做完一遍导入、读配置、
    甚至写会话文件（init_sessions 在导入期就跑了），有可能覆盖掉
    正在服务的那个实例的会话。所以必须在更早的位置拦住。
    """
    if allow_second:
        print("[DeckMeido] 单实例检查被显式跳过", file=sys.stderr)
        return
    if not already_serving(port):
        return
    print(
        f"[DeckMeido] 端口 {port} 上已有一份后端在服务，本进程退出。\n"
        f"            要重启后端请先关掉正在运行的那一份（托盘菜单 → 退出）。",
        file=sys.stderr,
    )
    raise SystemExit(3)

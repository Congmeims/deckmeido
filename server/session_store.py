"""会话的进程内状态与落盘。

原来这些都在 main.py 里，读写的是 main 的模块级全局（current_sid / _turn /
_sess_epoch ...）。那种写法一旦想拆文件就会踩两个坑：`from main import current_sid`
只在导入那一刻取值（会话切走后仍然是旧值），而 `global current_sid` 在别的模块里
不生效（赋值只改自己模块的名字）。

所以这里把状态收进一个对象，谁用谁 `from .session_store import store`，
读写的永远是同一份。main.py 只负责在合适的时候改 store 的字段。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


class SessionStore:
    """当前会话的内存状态 + 会话文件的读写"""

    def __init__(self) -> None:
        self.current_sid = "default"
        self.history: list[dict] = []
        # 会话代次：新建 / 切换 / 删除会话时 +1。
        # 一轮对话开始时快照 epoch，中途被别的端切走会话也能认出来，
        # 从而把结果写回原会话、不污染新会话（否则会出现「新会话回了上一个问题」）
        self.epoch = 0
        # 当前这一轮对话的归属：sid/epoch + 发起端，供 save / compact / 确认操作判断
        self.turn: dict = {"sid": "", "epoch": 0, "active": False, "client": "", "device": ""}
        # 已被用户删除的会话号。删除动作只删文件，而「正在跑的那一轮」手里还捏着这个
        # sid：收尾时 save_session_snapshot(turn_sid) / 流式节流的 _write_streaming
        # 都会把文件原样写回来 —— 用户看到的就是「删了一个，冒出两个会话」。
        # 这里记一笔，所有落盘入口先查名单，删掉的会话绝不允许复活。
        # 只在启动时为空（重启后文件确实不存在了，也没人在跑老那一轮）。
        self.deleted_sids: set[str] = set()
        # 会话文件的全部落盘操作都在这一把锁里串行执行。
        # 流式回调（每 1 秒一次）、正式写入、_streaming 清理可能是并发的：
        #   1. 共用同一个 "<sid>.json.tmp" 临时文件 —— 两个线程同时写它，A 刚写完
        #      B 又覆盖，A 再 os.replace 就是把 B 的半截内容换上去，文件损坏；
        #   2. 更常见的是「读到旧数据再整体写回」：A 读到 54 条，B 在同一时刻写入
        #      55 条，A 随后把自己手里的 54 条写回去 —— 刚生成的回复凭空消失。
        #      _write_streaming 正是「读-改-写 _streaming 字段」，最容易踩第 2 条。
        # 加锁后这些读-改-写变成原子序列，最多是内容旧一点，绝不会互相抹掉。
        self.lock = threading.RLock()
        # 压缩游标 {sid: {"summary": str, "upto": int}}：
        # 只压缩「发给模型的上下文」，绝不删界面历史
        self.compact_map: dict[str, dict] = {}

    # ---------- 代次与轮次 ----------

    def bump_epoch(self) -> None:
        """会话被新建 / 切换 / 删除时调用：让在跑的老一轮认得自己被切走了"""
        self.epoch += 1

    def turn_detached(self, epoch: int) -> bool:
        """这轮开始时快照的 epoch 是否已过期（会话被切走或重建）"""
        return epoch != self.epoch

    def turn_tag(self) -> dict:
        """给广播消息挂上本轮所属会话号；前端据此丢弃不属于当前会话的流式片段。

        发起端自带的 turn_id 也一并带上：插件要认出「哪些 δ 是我这一轮的」，
        只看 sid 不行——同一会话里别的端提问也是这个 sid。
        """
        tag: dict = {}
        if self.turn.get("active") and self.turn.get("sid"):
            tag["sid"] = self.turn["sid"]
        if self.turn.get("turn_id"):
            tag["turn_id"] = self.turn["turn_id"]
        return tag

    # ---------- 压缩游标 ----------

    def compact_for(self, sid: str) -> dict:
        """取某个会话的压缩游标（没有就返回空游标）"""
        cur = self.compact_map.get(sid or "")
        if not isinstance(cur, dict):
            return {"summary": "", "upto": 0}
        return {"summary": cur.get("summary") or "",
                "upto": max(0, int(cur.get("upto") or 0))}

    def set_compact(self, sid: str, summary: str, upto: int) -> None:
        self.compact_map[sid or self.current_sid] = {
            "summary": summary or "", "upto": max(0, int(upto or 0))}

    def drop_compact(self, sid: str) -> None:
        self.compact_map.pop(sid, None)

    # ---------- 路径 ----------

    def sess_path(self, sid: str) -> Path:
        return SESSIONS_DIR / f"{sid}.json"

    def fresh_sid(self) -> str:
        """生成一个目录里绝对不存在的会话号。

        直接 token_hex(4) 理论上够随机，但启动路径里出现过「老文件改名 .broken 后
        又用同一 sid 重建」的情况，新会话写回去正好覆盖掉还没救回来的历史。
        这里显式重试，撞号就换一个。
        """
        for _ in range(20):
            sid = secrets.token_hex(4)
            if not self.sess_path(sid).exists() and sid not in self.deleted_sids:
                return sid
        return secrets.token_hex(8)

    # ---------- 落盘 ----------

    def save_current(self) -> None:
        """把当前会话历史写盘，标题取第一条用户消息"""
        self.write_session(self.current_sid, self.history)

    def save_snapshot(self, sid: str, hist: list[dict]) -> bool:
        """把指定快照写回指定会话文件（会话中途被切走时，用这招把结果送回原会话）"""
        self.write_session(sid or self.current_sid, hist)
        return True

    def write_session(self, sid: str, hist: list[dict]) -> None:
        """原子写入一个会话文件。

        磁盘历史比本次要写的多时怎么办？答案是照写。以前这里做「缩水写合并」：
        用磁盘老历史做底、把内存里多出来的消息追加到末尾。它直接导致删不掉消息——
        用户删掉会话里几条，落盘时历史条数减半，触发合并，磁盘上被删的那几条
        又被拼了回来。合并还要求内存里至少有一条消息能在磁盘上找到锚点，
        删得多一点（比如清空重开）就直接判成「无法合并」并保留磁盘内容，
        于是界面上删干净了、重启后又全回来了。

        内存里的 history 才是唯一真相：用户删除、编辑、重排都发生在它上面，
        它写什么磁盘就存什么。真正需要防的只是「拿空历史覆盖掉有内容的文件」，
        那是启动时序上的竞态，不是合并能解决的问题——留着那道空写保护即可。
        """
        with self.lock:
            if sid in self.deleted_sids:
                # 会话已删除：迟到的落盘一律作废，否则等于把删掉的会话又变出来
                return
            p = self.sess_path(sid)
            created = None
            old_n = 0
            old_hist: list[dict] = []
            if p.exists():
                try:
                    old = json.loads(p.read_text(encoding="utf-8"))
                    created = old.get("created")
                    old_hist = old.get("history") or []
                    old_n = len(old_hist)
                except Exception as e:
                    # 解析失败不能当成「磁盘是空的」：下面的空写保护会因此失效，
                    # 内存恰好为空时就把整份历史覆盖掉了。留档后照常写入新内容
                    print(f"[DeckMeido] 会话 {sid} 读取失败（{e}），按新内容覆盖",
                          file=sys.stderr)
                    try:
                        p.rename(p.with_name(f"{p.name}.broken-{int(time.time())}"))
                    except OSError:
                        pass
                    created, old_n, old_hist = None, 0, []

            # 内存为空 = 刚建的新会话 / 启动时序竞态，绝不能拿空内容覆盖磁盘
            if not hist and old_n > 0:
                print(f"[DeckMeido] 拒绝空写入：会话 {sid} 磁盘有 {old_n} 条，"
                      f"内存为空，保持磁盘内容不变", file=sys.stderr)
                return

            data = {"id": sid, "created": created or time.time(), "updated": time.time(),
                    "history": hist}
            set_title(data, hist)
            # 压缩游标必须一起落盘：否则重启后模型重新吃到全量历史，
            # 白白撑爆上下文窗口，又要再压缩一遍
            comp = self.compact_for(sid)
            if comp["upto"] or comp["summary"]:
                data["compact"] = comp
            # 原子写：先写临时文件再 replace。直接 write_text 覆盖时如果进程被杀/连接中断，
            # 会留下半个 JSON，下次解析失败就等于整轮对话丢了
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, p)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

    def read_session(self, sid: str) -> dict:
        """读一个会话文件，读不到/解析失败返回空 dict"""
        p = self.sess_path(sid)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def rewrite_session(self, sid: str, data: dict) -> None:
        """把改过的会话 dict 原子写回（锁内调用方已持有锁也可以）

        写失败不吞掉：调用方（清 _streaming、改标题）需要知道没写成功，
        否则会以为已经落盘，下次启动又恢复出重复内容。
        """
        p = self.sess_path(sid)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


store = SessionStore()


def set_title(data: dict, hist: list[dict]) -> None:
    """给没有标题的空会话补一个带时间的标题，避免侧栏出现两个一模一样的「新会话」"""
    if data.get("title") and data["title"] != "新会话":
        return
    first_user = next((m.get("content") for m in hist
                       if m.get("role") == "user" and str(m.get("content") or "").strip()), None)
    if first_user:
        data["title"] = str(first_user).strip().replace("\n", " ")[:24]
    else:
        data["title"] = "新会话 " + time.strftime("%H:%M", time.localtime(data.get("created") or time.time()))

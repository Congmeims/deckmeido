"""TTS 语音播报：调用 Deck 内置 espeak-ng"""
from __future__ import annotations

import asyncio
import re

# 播报前清洗掉不适合读出来的内容
_MD_RE = re.compile(r"[*_`#>\[\]]|https?://\S+")


def _clean(text: str, limit: int) -> str:
    text = _MD_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class TTS:
    def __init__(self, enable: bool, engine: str, voice: str, speed: int, max_chars: int):
        self.enable = enable
        self.engine = engine
        self.voice = voice
        self.speed = speed
        self.max_chars = max_chars
        self._proc: asyncio.subprocess.Process | None = None

    async def speak(self, text: str):
        """异步播报，新的播报会打断旧的"""
        if not self.enable:
            return
        text = _clean(text, self.max_chars)
        if not text:
            return
        await self.stop()
        args = [self.engine]
        if self.engine.startswith("espeak"):
            args += ["-v", self.voice, "-s", str(self.speed)]
        args.append(text)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.enable = False

    async def stop(self):
        proc, self._proc = self._proc, None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            # kill 之后必须再 wait 一次：不等就丢引用会留下僵尸进程，
            # 而每次播报前都要 stop 一遍
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

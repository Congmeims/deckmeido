import { definePlugin, call, addEventListener, removeEventListener } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, TextField, ToggleField, Focusable } from "@decky/ui";

// 后端在不在、截图能不能用、代理走哪条路，由插件后端探测
type Status = {
  install_dir: string;
  config_ok: boolean;
  port: number;
  token_ok: boolean;
  gamescope: boolean;
  capture_ok: boolean;
  capture_reason: string;
  uid: number;
  as_user: string;
  proxy: string;
};

// 插件后端推上来的流式消息
type Delta =
  | { kind: "delta"; text: string }
  | { kind: "final"; text: string; cancelled?: boolean }
  // 「正在读文件：…」这类过程提示，只显示在状态行
  | { kind: "tool"; text: string }
  // 后端一轮结束后的状态广播，只用来确认「已经空闲」
  | { kind: "status"; running: boolean };

type Turn = { role: "user" | "assistant"; text: string; streaming?: boolean };

const CARD: React.CSSProperties = {
  // QAM 只有 ~300px 宽，字号和留白都按窄栏调，长词强制换行
  padding: "8px 10px",
  marginBottom: 6,
  borderRadius: 4,
  fontSize: 12,
  lineHeight: 1.45,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  overflowWrap: "anywhere",
};

const ERR_COLOR = "#e06c75";
const DIM = "#8b929a";

export default definePlugin(() => ({
  name: "DeckMeido",
  titleView: <div>DeckMeido</div>,
  content: <Content />,
  icon: <div />,
}));

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string>("");
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // 当前这一轮正在做什么（后端 tool_activity 的原文），空表示还没开始动手
  const [activity, setActivity] = useState("");
  const [withShot, setWithShot] = useState(true);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 流式正文先在 ref 里攒，避免每个 token 都触发一次全量状态重排
  const curRef = useRef<string>("");
  const busyRef = useRef(false);

  const refreshStatus = async () => {
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      return st;
    } catch (e) {
      setError(`插件后端未就绪：${e}`);
      return null;
    }
  };

  // 流式内容从插件后端来（前端不直连 :18765，绕开 CEF 的 CSP 限制）
  useEffect(() => {
    const onDelta = (msg: Delta) => {
      if (msg.kind === "status") {
        // 后端空了但界面还转着：说明这轮没收到 final（比如连接断了），先把按钮还回来
        if (!msg.running && busyRef.current) {
          setBusyBoth(false);
          setError("这一轮没有收到答复，可能是后端断开了");
        }
        return;
      }
      if (!busyRef.current) return;
      if (msg.kind === "tool") {
        setActivity(msg.text);
        return;
      }
      if (msg.kind === "delta") {
        curRef.current += msg.text;
        flushAssistant(false);
      } else {
        curRef.current = msg.text;
        finishTurn(msg.cancelled);
      }
    };
    addEventListener<[Delta]>("deckmeido_delta", onDelta);
    return () => removeEventListener("deckmeido_delta", onDelta);
  }, []);

  useEffect(() => {
    refreshStatus();
  }, []);

  // 新内容进来就滚到底，否则窄栏里看不到最新一行
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  // 忙碌计时：长时间没动静时界面要给个「还在跑」的反馈
  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now();
    setElapsed(0);
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const setBusyBoth = (v: boolean) => {
    busyRef.current = v;
    setBusy(v);
  };

  const flushAssistant = (final: boolean) => {
    const text = curRef.current;
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === "assistant") {
        return [...prev.slice(0, -1), { role: "assistant", text, streaming: !final }];
      }
      return [...prev, { role: "assistant", text, streaming: !final }];
    });
  };

  const finishTurn = (cancelled?: boolean) => {
    flushAssistant(true);
    setBusyBoth(false);
    setActivity("");
    if (cancelled) setError("这一轮已中断");
  };

  const ask = async () => {
    const text = input.trim();
    if (!text || busyRef.current) return;
    setError("");
    setActivity("");
    setTurns((prev) => [...prev, { role: "user", text }]);
    setInput("");
    curRef.current = "";
    setBusyBoth(true);

    try {
      const st = status ?? (await refreshStatus());
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");

      let image: string | null = null;
      if (withShot) {
        const shot = (await call<[], { ok: boolean; image?: string; error?: string }>(
          "screenshot"
        )) as { ok: boolean; image?: string; error?: string };
        if (shot.ok && shot.image) {
          image = shot.image;
        } else {
          // 截图失败不阻断提问：这一轮退回纯文字，并在界面上说清楚原因
          setError(`截图失败，这轮只发文字：${shot.error ?? "未知原因"}`);
        }
      }

      const res = (await call<[string, string | null], { ok: boolean; error?: string }>(
        "start_turn",
        text,
        image
      )) as { ok: boolean; error?: string };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

      // 正常情况下面的那个 final 事件会先把 busy 关掉；这里是超时兜底
      const fin = (await call<[], { ok: boolean; text?: string; error?: string }>(
        "wait_final"
      )) as { ok: boolean; text?: string; error?: string };
      if (busyRef.current) {
        if (fin.ok && fin.text) {
          curRef.current = fin.text;
          finishTurn(false);
        } else {
          throw new Error(fin.error ?? "没有收到答复");
        }
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusyBoth(false);
      setActivity("");
      if (curRef.current) flushAssistant(true);
    }
  };

  const cancel = async () => {
    setBusyBoth(false);
    setActivity("");
    try {
      await call<[], { ok: boolean; error?: string }>("cancel");
    } catch {
      /* 断开本身已经够用，取消失败不再打扰用户 */
    }
    setError("这一轮已中断");
  };

  return (
    <PanelSection title="DeckMeido">
      <PanelSectionRow>
        <div style={{ fontSize: 10, color: DIM, lineHeight: 1.5 }}>
          {busy && activity
            ? activity
            : status
              ? status.capture_ok
                ? "游戏模式抓帧可用"
                : status.capture_reason || "当前抓不到画面"
              : "正在探测后端…"}
          {status && !(busy && activity) && ` · 端口 ${status.port} · 转发 ${status.proxy}`}
        </div>
      </PanelSectionRow>

      <PanelSectionRow>
        <TextField
          label="问点什么"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ToggleField
          label="带画面提问"
          description="抓当前游戏画面一起发给 AI；抓不到时这一轮自动退回纯文字"
          checked={withShot}
          onChange={setWithShot}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={busy ? cancel : ask}>
          {busy ? `中断（${elapsed}s）` : "问 AI"}
        </ButtonItem>
      </PanelSectionRow>

      {error && (
        <PanelSectionRow>
          <div style={{ ...CARD, color: ERR_COLOR, background: "rgba(224,108,117,.1)" }}>
            {error}
          </div>
        </PanelSectionRow>
      )}

      {turns.length > 0 && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setTurns([])}>
            清空这段对话记录
          </ButtonItem>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <Focusable
          ref={scrollRef as any}
          style={{ maxHeight: 260, overflowY: "auto", display: "block" }}
        >
          {turns.map((t, i) => (
            <div
              key={i}
              style={{
                ...CARD,
                background: t.role === "user" ? "rgba(255,255,255,.06)" : "transparent",
                borderLeft:
                  t.role === "assistant" ? "2px solid #5aa9e6" : "2px solid transparent",
              }}
            >
              {t.text}
              {t.streaming && <span style={{ color: DIM }}>▍</span>}
            </div>
          ))}
        </Focusable>
      </PanelSectionRow>
    </PanelSection>
  );
}

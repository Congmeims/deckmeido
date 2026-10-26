import { definePlugin, call } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, TextField, Focusable } from "@decky/ui";

// 后端在不在、截图能不能用，由插件后端探测（它知道安装目录和 gamescope 状态）
type Status = {
  install_dir: string;
  config_ok: boolean;
  port: number;
  gamescope: boolean;
  capture_ok: boolean;
  uid: number;
  as_user: string;
  token: string;
};

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
  const [withShot, setWithShot] = useState(true);

  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 增量文本先在 ref 里攒，避免每个 token 都触发一次全量状态重排
  const curRef = useRef<string>("");

  useEffect(() => {
    (async () => {
      try {
        const st = (await call<[], Status>("get_status")) as Status;
        setStatus(st);
      } catch (e) {
        setError(`插件后端未就绪：${e}`);
      }
    })();
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  // 新内容进来就滚到底，否则窄栏里看不到最新一行
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

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

  const ask = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setError("");
    setBusy(true);
    setTurns((prev) => [...prev, { role: "user", text }]);
    setInput("");
    curRef.current = "";

    let sock: WebSocket;
    try {
      const st = status ?? ((await call<[], Status>("get_status")) as Status);
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");

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

      // 建连接并等它 open：这一步失败就是连不上后端，直接抛给外层
      sock = new WebSocket(
        `ws://127.0.0.1:${st.port}/ws?token=${encodeURIComponent(st.token)}`
      );
      wsRef.current = sock;
      await new Promise<void>((resolve, reject) => {
        const s = sock as WebSocket;
        s.onopen = () => resolve();
        s.onerror = () => reject(new Error(`连不上后端 :${st.port}`));
      });

      sock.onmessage = (ev) => {
        let msg: any;
        try {
          msg = JSON.parse(ev.data as string);
        } catch {
          return;
        }
        if (msg.type === "delta") {
          // 后端流式增量：final=True 的那条只是收尾信号，text 为空
          if (msg.text) {
            curRef.current += String(msg.text);
            flushAssistant(false);
          }
          if (msg.final) {
            flushAssistant(true);
            setBusy(false);
          }
        } else if (msg.type === "assistant_message") {
          // 完整答复兜底覆盖，避免增量丢字
          if (msg.text) curRef.current = String(msg.text);
          flushAssistant(true);
          setBusy(false);
        }
      };
      sock.onclose = () => {
        wsRef.current = null;
        setBusy(false);
        // 连接断了但还没收到 final，说明这一轮没走完
        flushAssistant(true);
      };

      sock.send(JSON.stringify({ type: "chat", text, image, workspace: "/home/deck" }));
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
      wsRef.current?.close();
      wsRef.current = null;
    }
  };

  const cancel = async () => {
    wsRef.current?.close();
    wsRef.current = null;
    setBusy(false);
    // 后端的 /api/cancel 会掐掉这一轮并回收子进程，比单纯断 ws 干净
    try {
      const st = status ?? ((await call<[], Status>("get_status")) as Status);
      await fetch(`http://127.0.0.1:${st.port}/api/cancel`, {
        method: "POST",
        headers: { "X-Token": st.token },
      });
    } catch {
      /* 断链本身已经够用，取消失败不再打扰用户 */
    }
    setError("这一轮已中断");
  };

  return (
    <PanelSection title="DeckMeido">
      {status && (
        <PanelSectionRow>
          <div style={{ fontSize: 10, color: DIM, lineHeight: 1.5 }}>
            {status.capture_ok ? "游戏模式抓帧可用" : "当前抓不到画面（不在游戏模式？）"}
            {" · 端口 "}
            {status.port}
            {" · uid "}
            {status.uid}
          </div>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <TextField
          label="问点什么"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setWithShot((v) => !v)}>
          {withShot ? "带画面提问：开" : "带画面提问：关"}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={busy ? cancel : ask}>
          {busy ? "中断" : "问 AI"}
        </ButtonItem>
      </PanelSectionRow>

      {error && (
        <PanelSectionRow>
          <div style={{ ...CARD, color: ERR_COLOR, background: "rgba(224,108,117,.1)" }}>
            {error}
          </div>
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

import { definePlugin, call, addEventListener, removeEventListener } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { Navigation, PanelSection, PanelSectionRow, ButtonItem, TextField, ToggleField, Focusable } from "@decky/ui";

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
  // 虚拟手柄能不能用（/dev/uinput 存在且可写）
  gamepad: boolean;
  // 本机鉴权：false 时面板只显示解锁框
  unlocked: boolean;
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

// 面板上的手柄键位，分两排：上面是 ABXY + 扳机，下面是方向键 + 菜单键
const PAD_ROWS: { key: string; label: string }[][] = [
  [
    { key: "y", label: "Y" },
    { key: "x", label: "X" },
    { key: "b", label: "B" },
    { key: "a", label: "A" },
  ],
  [
    { key: "lb", label: "LB" },
    { key: "rb", label: "RB" },
    { key: "lt", label: "LT" },
    { key: "rt", label: "RT" },
  ],
  [
    { key: "l3", label: "L3" },
    { key: "r3", label: "R3" },
    { key: "start", label: "≡" },
    { key: "select", label: "◱" },
    { key: "guide", label: "Xbox" },
    { key: "share", label: "分享" },
  ],
  [
    { key: "up", label: "↑" },
    { key: "down", label: "↓" },
    { key: "left", label: "←" },
    { key: "right", label: "→" },
  ],
];

const PAD_BTN: React.CSSProperties = {
  flex: 1,
  // Steam 的字体在 QAM 里偏大，按钮给足高度才好用手柄点
  padding: "10px 0",
  fontSize: 13,
  borderRadius: 4,
  border: "1px solid rgba(255,255,255,.14)",
  background: "rgba(255,255,255,.06)",
  color: "#fff",
  textAlign: "center",
};

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
  // 手柄区最近一次操作的结果，只用来给一句提示
  const [padMsg, setPadMsg] = useState("");
  // 解锁：令牌输入框 + 后端给的票据（之后每次调用都要带上）
  const [tokenInput, setTokenInput] = useState("");
  const [ticket, setTicket] = useState("");

  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 流式正文先在 ref 里攒，避免每个 token 都触发一次全量状态重排
  const curRef = useRef<string>("");
  const busyRef = useRef(false);

  const refreshStatus = async () => {
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      // 后端说没解锁就把本地票据丢掉：插件可能重启过，旧票据已经作废，
      // 留着只会让每次调用都报「票据不对」
      if (!st.unlocked) setTicket("");
      return st;
    } catch (e) {
      setError(`插件后端未就绪：${e}`);
      return null;
    }
  };

  const unlock = async () => {
    const tk = tokenInput.trim();
    if (!tk) return;
    try {
      const r = (await call<[string], { ok: boolean; ticket?: string; error?: string }>(
        "unlock",
        tk
      )) as { ok: boolean; ticket?: string; error?: string };
      if (!r.ok || !r.ticket) {
        setError(r.error ?? "解锁失败");
        return;
      }
      setTicket(r.ticket);
      setTokenInput("");
      setError("");
      await refreshStatus();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  const lock = async () => {
    try {
      await call<[string], { ok: boolean }>("lock", ticket);
    } catch {
      /* 锁不上不影响界面回到解锁框 */
    }
    setTicket("");
    setTurns([]);
    setError("");
    await refreshStatus();
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
    setInput("");
    await runTurn(text, withShot);
  };

  /**
   * 发一轮对话。grab=true 时连画面一起发；hideQam=true 时先收起 QAM 再抓帧
   * （否则截到的是「QAM 半屏 + 游戏半屏」）。
   */
  const runTurn = async (text: string, grab: boolean, hideQam = false) => {
    if (busyRef.current) return;
    setError("");
    setActivity("");
    setTurns((prev) => [...prev, { role: "user", text }]);
    curRef.current = "";
    setBusyBoth(true);

    try {
      const st = status ?? (await refreshStatus());
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");
      if (!st.unlocked || !ticket) throw new Error("插件未解锁，先输入访问令牌");

      let image: string | null = null;
      if (grab) {
        // 先收面板：CloseSideMenus 走的是 Steam 的 MenuStore，等于替用户按了
        // 一次「关闭侧栏」，和按 B 不是一回事（B 在菜单里是「回上一级」）
        if (hideQam) Navigation.CloseSideMenus();
        const shot = (await call<[string, boolean], { ok: boolean; image?: string; error?: string }>(
          "screenshot",
          ticket,
          hideQam
        )) as { ok: boolean; image?: string; error?: string };
        // 抓完立刻把 QAM 弹回来：后面还有几十秒的生成时间，用户不该盯着一片游戏画面
        if (hideQam) Navigation.OpenQuickAccessMenu();
        if (shot.ok && shot.image) {
          image = shot.image;
        } else {
          // 截图失败不阻断提问：这一轮退回纯文字，并在界面上说清楚原因
          setError(`截图失败，这轮只发文字：${shot.error ?? "未知原因"}`);
        }
      }

      const res = (await call<[string, string | null, string], { ok: boolean; error?: string }>(
        "start_turn",
        text,
        image,
        ticket
      )) as { ok: boolean; error?: string };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

      // 正常情况下面的那个 final 事件会先把 busy 关掉；这里是超时兜底
      const fin = (await call<[number, string], { ok: boolean; text?: string; error?: string }>(
        "wait_final",
        900,
        ticket
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

  /**
   * 一键查攻略：语音/打字都不方便时，直接拿当前画面去问。
   * 提示词写死「先搜再总结」，是因为攻略类问题答案更新快，
   * 单靠模型记忆容易给出过期的打法或道具位置。
   */
  const guide = async () => {
    if (busyRef.current) return;
    await runTurn(
      "看这个游戏画面，判断我在哪个游戏、卡在什么位置，然后先联网搜索最新攻略，再总结成几条可以直接照做的步骤（控制在 200 字内）。",
      true,
      true
    );
  };

  const cancel = async () => {
    setBusyBoth(false);
    setActivity("");
    try {
      await call<[string], { ok: boolean; error?: string }>("cancel", ticket);
    } catch {
      /* 断开本身已经够用，取消失败不再打扰用户 */
    }
    setError("这一轮已中断");
  };

  const padPress = async (key: string) => {
    if (!status?.gamepad) {
      setPadMsg("/dev/uinput 不可用，手柄按键发不出去");
      return;
    }
    try {
      const r = (await call<[string, number, string], { ok: boolean; error?: string }>(
        "gamepad_press",
        key,
        80,
        ticket
      )) as { ok: boolean; error?: string };
      setPadMsg(r.ok ? `已发送 ${key.toUpperCase()}` : (r.error ?? "发送失败"));
    } catch (e: any) {
      setPadMsg(String(e?.message ?? e));
    }
  };

  // 未解锁：面板只留解锁框，提问和手柄一律不显示，避免用户点了才发现没权限
  if (status && !status.unlocked) {
    return (
      <PanelSection title="DeckMeido">
        <PanelSectionRow>
          <div style={{ fontSize: 10, color: DIM, lineHeight: 1.5 }}>
            插件以 root 运行，需要先输入访问令牌解锁
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="访问令牌"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={unlock}>
            解锁
          </ButtonItem>
        </PanelSectionRow>
        {error && (
          <PanelSectionRow>
            <div style={{ ...CARD, color: ERR_COLOR, background: "rgba(224,108,117,.1)" }}>
              {error}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
    );
  }

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
        <ButtonItem layout="below" onClick={guide}>
          查攻略（看当前画面）
        </ButtonItem>
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

      <PanelSectionRow>
        <div style={{ fontSize: 10, color: DIM, marginBottom: 2 }}>
          {padMsg || "模拟手柄按键（发给前台游戏）"}
        </div>
      </PanelSectionRow>

      {PAD_ROWS.map((row, ri) => (
        <PanelSectionRow key={ri}>
          <Focusable style={{ display: "flex", gap: 4 }}>
            {row.map((b) => (
              <div
                key={b.key}
                style={{ ...PAD_BTN, opacity: status?.gamepad ? 1 : 0.4 }}
                onClick={() => padPress(b.key)}
              >
                {b.label}
              </div>
            ))}
          </Focusable>
        </PanelSectionRow>
      ))}

      {turns.length > 0 && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setTurns([])}>
            清空这段对话记录
          </ButtonItem>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={lock}>
          锁定（退出解锁）
        </ButtonItem>
      </PanelSectionRow>

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

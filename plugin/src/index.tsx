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
  // 本机局域网地址：面板第二行显示成 ip:port
  ip: string;
  // 女仆核心（后端自身）活没活着
  core_ok: boolean;
  core_reason: string;
  core_mode: string;
  // 工作区绝对路径：面板底部显示，让主人知道 agent 能碰哪些文件
  workspace: string;
  // 虚拟手柄能不能用（/dev/uinput 存在且可写）
  gamepad: boolean;
  // 设置区：语音播报（后端 tts.enable）
  tts: boolean;
  // 底部显示：后端版本号，后端连不上时是插件自己的版本
  version: string;
  // 本机鉴权：false 时面板只显示解锁框
  unlocked: boolean;
  // 后端记着的那张票据（没解锁时是空串）。
  // QAM 面板一关一开，React 组件会重新挂载、useState 归零；靠这个把票据接回来，
  // 否则「截屏询问」先收面板再展开时票据就丢了，明明解锁了还报未解锁
  ticket: string;
};

// 面板展示的一条对话（所有端的提问和回复都在这）
type Item = {
  role: "user" | "assistant";
  text: string;
  // 来源：web / mobile / client / qam，空表示老数据没记
  client: string;
  ts: number;
};

// 插件后端推上来的流式消息
type Delta =
  | { kind: "delta"; text: string }
  | { kind: "final"; text: string; cancelled?: boolean }
  // 「正在读文件：…」这类过程提示，只显示在状态行
  | { kind: "tool"; text: string }
  // 后端一轮结束后的状态广播，只用来确认「已经空闲」
  | { kind: "status"; running: boolean }
  // 全部对话（history 对齐或新消息追加）
  | { kind: "thread"; items: Item[] };

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

/**
 * QAM 里渲染 markdown。后端给的是 markdown，网页端有 renderMd 处理，
 * 但 QAM 不能直接塞 HTML（_dangerouslySetInnerHTML 在 Decky 的 CJS 沙箱里
 * 不可靠，而且模型的输出未经清洗），所以在这里按行切成小块，用 React 元素渲染。
 *
 * 只认攻略里真会用到的几种：标题、有序/无序列表、``` 代码块、**加粗**、`行内码`。
 * 表格和图片直接按原文保留：QAM 只有 ~300px 宽，表格横着放不下，图片也加载不了。
 */
type Block =
  | { t: "h"; text: string }
  | { t: "ul"; items: string[] }
  | { t: "ol"; items: string[] }
  | { t: "code"; text: string }
  | { t: "p"; text: string };

function parseMd(src: string): Block[] {
  const out: Block[] = [];
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let para: string[] = [];

  const flushPara = () => {
    if (para.length) {
      out.push({ t: "p", text: para.join("\n") });
      para = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 代码块要整段吃掉，里面的 # 和 - 不能当成结构
    const fence = line.match(/^\s*```/);
    if (fence) {
      flushPara();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      out.push({ t: "code", text: buf.join("\n") });
      continue;
    }
    const h = line.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/);
    if (h) {
      flushPara();
      out.push({ t: "h", text: h[1] });
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.+)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ul || ol) {
      flushPara();
      const list = (ul ? "ul" : "ol") as "ul" | "ol";
      const last = out[out.length - 1];
      const items = ul ? [ul[1]] : [ol![1]];
      // 连续的列表行合成一个块，否则每条都单独排一段会很散
      while (i + 1 < lines.length) {
        const nx = lines[i + 1];
        const mu = nx.match(/^\s*[-*+]\s+(.+)$/);
        const mo = nx.match(/^\s*\d+[.)]\s+(.+)$/);
        if (ul && mu) items.push(mu[1]);
        else if (ol && mo) items.push(mo[1]);
        else break;
        i++;
      }
      if (last && last.t === list) last.items.push(...items);
      else out.push({ t: list, items });
      continue;
    }
    if (!line.trim()) {
      flushPara();
      continue;
    }
    para.push(line);
  }
  flushPara();
  return out;
}

/** 行内：`code`、**粗体**；其余原样（表格竖线、链接文字都留着可读） */
function inline(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tk = m[0];
    if (tk.startsWith("`")) {
      nodes.push(
        <code
          key={k++}
          style={{
            background: "rgba(255,255,255,.1)",
            borderRadius: 3,
            padding: "0 3px",
            fontSize: 11,
          }}
        >
          {tk.slice(1, -1)}
        </code>
      );
    } else {
      nodes.push(
        <b key={k++} style={{ color: "#fff" }}>
          {tk.slice(2, -2)}
        </b>
      );
    }
    last = m.index + tk.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function Md({ text }: { text: string }) {
  const blocks = parseMd(text);
  return (
    <>
      {blocks.map((b, i) => {
        if (b.t === "h") {
          return (
            <div
              key={i}
              style={{ fontWeight: 700, fontSize: 12.5, color: "#fff", margin: "6px 0 3px" }}
            >
              {inline(b.text)}
            </div>
          );
        }
        if (b.t === "code") {
          return (
            <pre
              key={i}
              style={{
                margin: "4px 0",
                padding: "6px 8px",
                background: "rgba(0,0,0,.35)",
                borderRadius: 4,
                fontSize: 11,
                lineHeight: 1.4,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                overflowX: "auto",
              }}
            >
              <code>{b.text}</code>
            </pre>
          );
        }
        if (b.t === "ul" || b.t === "ol") {
          const Tag = b.t;
          return (
            <Tag key={i} style={{ margin: "3px 0", paddingLeft: 18 }}>
              {b.items.map((it, j) => (
                <li key={j} style={{ margin: "1px 0" }}>
                  {inline(it)}
                </li>
              ))}
            </Tag>
          );
        }
        return (
          <div key={i} style={{ margin: "3px 0" }}>
            {b.text.split("\n").map((ln, j) => (
              <div key={j}>{inline(ln)}</div>
            ))}
          </div>
        );
      })}
    </>
  );
}

// 来源标记。QAM 只有 ~300px 宽，用短一点的说法：
// 网页端可能来自 PC 也可能是 Deck 上的浏览器，统一叫「网页」
const CLIENT_LABEL: Record<string, string> = {
  web: "PC网页端",
  mobile: "手机网页端",
  client: "客户端",
  qam: "插件端",
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
  // 所有端的提问和回复，直接照后端推来的顺序显示
  const [items, setItems] = useState<Item[]>([]);
  // 本端这一轮正在生成的正文（还没进 items，收尾时由 final 落进去）
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // 当前这一轮正在做什么（后端 tool_activity 的原文），空表示还没开始动手
  const [activity, setActivity] = useState("");
  const [withShot, setWithShot] = useState(true);
  // 设置区默认收起，点「设置」才展开
  const [showSettings, setShowSettings] = useState(false);
  // 语音播报的实际值以后端为准：status 回来后同步过来，切换时先乐观更新
  const [tts, setTts] = useState(false);
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
      setTts(!!st.tts);
      // 票据以后端为准：面板重挂载后本地是空的，这里补回来；
      // 后端说没解锁（插件重启过、票据过期）就把本地那张丢掉
      setTicket(st.ticket || "");
      return st;
    } catch (e) {
      setError(`插件后端未就绪：${e}`);
      return null;
    }
  };

  // 拿票据：手动输入令牌后换取票据
  const applyTicket = async (tk: string) => {
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
  };

  const unlock = async () => {
    const tk = tokenInput.trim();
    if (!tk) return;
    try {
      await applyTicket(tk);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  // 流式内容从插件后端来（前端不直连 :18765，绕开 CEF 的 CSP 限制）
  useEffect(() => {
    const onDelta = (msg: Delta) => {
      if (msg.kind === "thread") {
        setItems(msg.items);
        return;
      }
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
        flushAssistant();
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

  // 解锁后连上后端，把「所有端」的提问和回复一次拉回来；
  // 再调一次 ready：history 那条 thread 可能在本面板首帧之前就推过了
  useEffect(() => {
    if (!ticket) return;
    (async () => {
      try {
        await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
        await call<[string], { ok: boolean }>("ready", ticket);
      } catch {
        /* 连不上会在 get_status 里体现，这里不重复报错 */
      }
    })();
  }, [ticket]);

  // 新内容进来就滚到底，否则窄栏里看不到最新一行
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items, streaming]);

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

  const flushAssistant = () => {
    setStreaming(curRef.current);
  };

  const finishTurn = (cancelled?: boolean) => {
    // 正文落进 items 由后端推来的 assistant_message（thread 事件）负责，
    // 这里只收掉本端的流式占位
    setStreaming("");
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
    // 用户那条由后端广播回来（带来源、带 ts），这里不抢先插一条，
    // 否则同一句话会先本地显示一次、再被所有端同步的消息覆盖
    curRef.current = "";
    setStreaming("");
    setBusyBoth(true);

    try {
      // 每次都重新问一次：面板重挂载后本地 ticket 是空的，靠这次拿回来
      const st = await refreshStatus();
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");
      if (!st.unlocked || !st.ticket) throw new Error("插件未解锁，先输入访问令牌");
      const tk = st.ticket;

      let image: string | null = null;
      let game: { appid?: string; name?: string } | null = null;
      if (grab) {
        // 先收面板：CloseSideMenus 走的是 Steam 的 MenuStore，等于替用户按了
        // 一次「关闭侧栏」，和按 B 不是一回事（B 在菜单里是「回上一级」）
        if (hideQam) Navigation.CloseSideMenus();
        const shot = (await call<
          [string, boolean],
          { ok: boolean; image?: string; error?: string; game?: { appid?: string; name?: string } }
        >("screenshot", tk, hideQam)) as {
          ok: boolean;
          image?: string;
          error?: string;
          game?: { appid?: string; name?: string };
        };
        // 抓完立刻把 QAM 弹回来：后面还有几十秒的生成时间，用户不该盯着一片游戏画面
        if (hideQam) Navigation.OpenQuickAccessMenu();
        if (shot.ok && shot.image) {
          image = shot.image;
        } else {
          // 截图失败不阻断提问：这一轮退回纯文字，并在界面上说清楚原因
          setError(`截图失败，这轮只发文字：${shot.error ?? "未知原因"}`);
        }
        game = shot.game ?? null;
      }

      // 游戏名从 Steam 侧拿到就直接写进问题里，免得模型看画面认错游戏、
      // 搜回来一堆别的游戏的攻略
      let body = text;
      const gname = game?.name ?? "";
      if (gname) {
        const aid = game?.appid ? `（appid ${game.appid}）` : "";
        body = `当前游戏：《${gname}》${aid}。\n${text}`;
      }

      const res = (await call<[string, string | null, string], { ok: boolean; error?: string }>(
        "start_turn",
        body,
        image,
        tk
      )) as { ok: boolean; error?: string };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

      // 正常情况下面的那个 final 事件会先把 busy 关掉；这里是超时兜底
      const fin = (await call<[number, string], { ok: boolean; text?: string; error?: string }>(
        "wait_final",
        900,
        tk
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
      setStreaming("");
    }
  };

  /**
   * 截屏询问：收面板 → 抓一帧当前画面 → 带图自动问 AI。
   * 提问写死「看我卡在哪一步」，是因为这个按钮就是给「现在过不去了」用的，
   * 用户不用描述场景，模型看画面更快；游戏名由后端从 Steam 取进提问里。
   */
  const shotAsk = async () => {
    if (busyRef.current) return;
    await runTurn(
      "看我卡在哪一步，说说现在这个画面里我该做什么，需要的话先联网查一下最新攻略，" +
        "再总结成几条可以直接照做的步骤，200 字以内。",
      true,
      true
    );
  };

  /** 语音播报开关：先按用户点的显示，后端拒绝再翻回去并提示 */
  const toggleTts = async (v: boolean) => {
    setTts(v);
    try {
      const r = (await call<[boolean, string], { ok: boolean; tts?: boolean; error?: string }>(
        "set_tts",
        v,
        ticket
      )) as { ok: boolean; tts?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setTts(!!r.tts);
    } catch (e: any) {
      setTts(!v);
      setError(`语音播报切换失败：${String(e?.message ?? e)}`);
    }
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

  // 未解锁：面板只留解锁框，提问一律不显示，避免用户点了才发现没权限
  if (status && !status.unlocked) {
    return (
      <PanelSection title="DeckMeido">
        <PanelSectionRow>
          <div style={{ fontSize: 10, color: DIM, lineHeight: 1.5 }}>
            插件以 root 运行，需要先输入访问令牌解锁（默认 meido）
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

  const captureLine = status
    ? status.capture_ok
      ? "自动截图可用"
      : `自动截图不可用${status.capture_reason ? `（${status.capture_reason}）` : ""}`
    : "正在探测后端…";
  const coreLine = status ? (status.core_ok ? "女仆核心 正常" : "女仆核心 异常") : "女仆核心 未知";
  const coreWhy = status && !status.core_ok ? status.core_reason : "";

  return (
    <PanelSection title="DeckMeido">
      <PanelSectionRow>
        <div style={{ fontSize: 10, lineHeight: 1.5 }}>
          <div style={{ color: DIM }}>
            <span style={{ color: status?.capture_ok ? DIM : ERR_COLOR }}>{captureLine}</span>
            {" · "}
            <span style={{ color: status?.core_ok ? DIM : ERR_COLOR }}>{coreLine}</span>
          </div>
          <div style={{ color: DIM }}>
            {status ? `${status.ip}:${status.port}` : "—"}
          </div>
          {coreWhy && <div style={{ color: ERR_COLOR }}>{coreWhy}</div>}
          {busy && activity && <div style={{ color: DIM }}>{activity}</div>}
        </div>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={busy ? cancel : shotAsk}>
          {busy ? `中断（${elapsed}s）` : "截屏询问"}
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
          style={{ maxHeight: 330, overflowY: "auto", display: "block" }}
        >
          {items.map((m, i) => {
            const tag = CLIENT_LABEL[m.client] ?? "";
            return (
              <div
                key={`${m.ts}-${i}`}
                style={{
                  ...CARD,
                  background: m.role === "user" ? "rgba(255,255,255,.06)" : "transparent",
                  borderLeft: m.role === "assistant" ? "2px solid #5aa9e6" : "2px solid transparent",
                }}
              >
                {tag && (
                  // 来源角标：所有端混在一起时，一眼看清这句是谁说的
                  <div style={{ fontSize: 9, color: DIM, marginBottom: 2 }}>
                    {tag}
                  </div>
                )}
                <Md text={m.text} />
              </div>
            );
          })}
          {streaming && (
            <div
              style={{
                ...CARD,
                borderLeft: "2px solid #5aa9e6",
              }}
            >
              {CLIENT_LABEL.qam && <div style={{ fontSize: 9, color: DIM, marginBottom: 2 }}>插件端</div>}
              <Md text={streaming} />
              <span style={{ color: DIM }}>▍</span>
            </div>
          )}
          {!items.length && !streaming && (
            <div style={{ ...CARD, color: DIM }}>
              还没有对话。点上面的「截屏询问」，或者直接在下面打字。
            </div>
          )}
        </Focusable>
      </PanelSectionRow>

      <PanelSectionRow>
        <TextField
          label="问点什么"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={busy ? cancel : ask}>
          {busy ? `中断（${elapsed}s）` : "发送"}
        </ButtonItem>
      </PanelSectionRow>

      {/* 设置默认收起：QAM 里平时只想要「问一句 / 看一眼」，太长会挡住对话 */}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setShowSettings((v) => !v)}>
          {showSettings ? "收起设置 ▴" : "设置 ▾"}
        </ButtonItem>
      </PanelSectionRow>

      {showSettings && (
        <>
          <PanelSectionRow>
            <ToggleField
              label="带画面询问"
              description="发送时抓当前游戏画面一起发给 AI；抓不到时这一轮自动退回纯文字"
              checked={withShot}
              onChange={setWithShot}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="语音播报回复"
              description="答复生成完在本机念一遍；和网页设置页的开关是同一个"
              checked={tts}
              onChange={toggleTts}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ fontSize: 10, color: DIM }}>
              后端 {status?.core_ok ? "已连接" : "未连接"} · 工作区{" "}
              {status?.workspace || "—"}
            </div>
          </PanelSectionRow>
        </>
      )}

      {/* 底部版本号：居中一行，版本取后端 /api/config，连不上时是插件自己的 */}
      <PanelSectionRow>
        <div style={{ fontSize: 10, color: DIM, textAlign: "center" }}>
          v{status?.version || "—"}
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}

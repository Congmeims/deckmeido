import { definePlugin, call, addEventListener, removeEventListener } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { Navigation, PanelSection, PanelSectionRow, ButtonItem, TextField, ToggleField, Focusable } from "@decky/ui";
import { GiTeapot } from "react-icons/gi";

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
  // 当前正在运行的游戏名（后端从 Steam 进程/日志读出），没在玩时为空
  game_name: string;
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
  // 随提问附带的画面（截屏询问/别的端发的图），后端存好后给的 /shots/ 路径
  image?: string;
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

// 对话区左右内边距：滚动条会吃掉几条像素，这里让出一点免得文字被裁。
// 竖直方向靠各条消息自己的 marginBottom 隔开，不再额外加 padding
const THREAD_PAD = "0 2px";

/**
 * 并排小按钮的样式：QAM 里两个 ButtonItem 塞进同一 flex 行会互相压住
 * （ButtonItem 外层固定 100% 宽、文字居中溢出），所以并排场景改用
 * 普通 div 仿按钮，宽高、圆角、底色都对着 GamepadUI 的按钮取值。
 * 外框固定在父级，内层撑满，这样 flex 分配多少就占多少。
 */
const FAKE_BTN: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  minHeight: 40,
  padding: "0 14px",
  borderRadius: 2,
  background: "rgba(255,255,255,.10)",
  color: "#dcdedf",
  fontSize: 14,
  whiteSpace: "nowrap",
  boxSizing: "border-box",
  cursor: "pointer",
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

function Md({ text, align }: { text: string; align?: "left" | "right" }) {
  const blocks = parseMd(text);
  return (
    // 对齐挂在最外层：用户消息靠右显示，自己说的话右边缘对齐才顺眼。
    // 列表项和代码块仍保持左对齐（右对齐的代码几乎没法读）
    <div style={{ textAlign: align ?? "left" }}>
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
                textAlign: "left",
              }}
            >
              <code>{b.text}</code>
            </pre>
          );
        }
        if (b.t === "ul" || b.t === "ol") {
          const Tag = b.t;
          return (
            <Tag key={i} style={{ margin: "3px 0", paddingLeft: 18, textAlign: "left" }}>
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
    </div>
  );
}

// 插件自己的版本号。构建时由 rollup 的 replace 从 package.json 注进来；
// 要求与主程序的 VERSION 保持一致（发布时一起改）
declare const __PLUGIN_VERSION__: string;
const PLUGIN_VERSION =
  typeof __PLUGIN_VERSION__ === "string" ? __PLUGIN_VERSION__ : "—";

// 截屏询问在面板上留的那句配文：真正的提示词有一百多字，
// 长显会把整个面板撑开，用户也不用读它
const SHOT_ASK_BRIEF = "看看这个，我该怎么做？";

// 后端还没读到模板时用的兜底：和后端 DEFAULT_ASK_TEMPLATE 保持一致
const DEFAULT_ASK_TPL =
  "看我卡在哪一步，说说现在这个画面里我该做什么。需要的话先联网查一下这款游戏的攻略，" +
  "再总结成几条可以直接照做的步骤，200 字以内。\n" +
  "不要剧透：只讲眼前这一步怎么过，不要提前说后面的剧情、结局或其他关卡的解法。";

export default definePlugin(() => ({
  name: "DeckMeido",
  titleView: <div>DeckMeido</div>,
  content: <Content />,
  // 图标外框占位与其它插件保持一致，只把墨迹放大，避免把标题推歪
  icon: (
    <div style={{ width: "18px", height: "18px", display: "flex", alignItems: "center", justifyContent: "center", flex: "none" }}>
      <GiTeapot size={20} style={{ transform: "scale(1.15)", transformOrigin: "center" }} />
    </div>
  ),
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
  // 「截屏询问」用的提示词模板，存在后端配置里（网页设置页改的也是这一份）
  const [askTpl, setAskTpl] = useState("");
  const [askTplEdit, setAskTplEdit] = useState("");
  // 本端最近一次抓到的画面（data: URL），只用于在面板里确认「刚才截到的是什么」
  const [shot, setShot] = useState<string>("");
  // 解锁：令牌输入框 + 后端给的票据（之后每次调用都要带上）
  const [tokenInput, setTokenInput] = useState("");
  const [ticket, setTicket] = useState("");
  // 探测进行中：没有结果时才显示「探测中」，避免把「连不上」说成「未知」
  const [probing, setProbing] = useState(true);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 流式正文先在 ref 里攒，避免每个 token 都触发一次全量状态重排
  const curRef = useRef<string>("");
  const busyRef = useRef(false);

  const refreshStatus = async () => {
    setProbing(true);
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      setTts(!!st.tts);
      // 票据以后端为准：面板重挂载后本地是空的，这里补回来；
      // 后端说没解锁（插件重启过、票据过期）就把本地那张丢掉
      setTicket(st.ticket || "");
      // 模板跟着票据一起取：没解锁时后端会拒绝，取不到就保持空（不影响默认模板生效）
      if (st.ticket) void loadAskTpl(st.ticket);
      return st;
    } catch (e: any) {
      const why = String(e?.message ?? e);
      setError(`插件后端未就绪：${why}`);
      return null;
    } finally {
      setProbing(false);
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

  // 重开机默认进游戏模式，后端不会自己起来。这里补一个入口，
  // 按下后插件以 deck 身份 spawn uvicorn，等到 /api/status 应答才返回
  const [starting, setStarting] = useState(false);
  // 新建会话的进行中标记：和「启动后端」分开，否则两个按钮会一起变成「正在…」
  const [newing, setNewing] = useState(false);
  const startBackend = async () => {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; error?: string; already?: boolean }>(
        "start_backend",
        ticket
      )) as { ok: boolean; error?: string; already?: boolean };
      if (!r.ok) {
        setError(r.error ?? "拉起后端失败");
        return;
      }
      await refreshStatus();
      await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
      await call<[string], { ok: boolean }>("ready", ticket);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setStarting(false);
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

  // 列表是倒序（最新在最上），新内容进来就滚回顶部，
  // 否则窄栏里还停在旧消息那一屏，等于没看到
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = 0;
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
   * 收拾完面板再开回来。
   *
   * 不带参的 OpenQuickAccessMenu() 是按「上一次停留的那一层」恢复的：截屏这一路
   * 刚 CloseSideMenus 过，很多时候回来就落在通知中心。
   * 第一个参数是标签页序号，这里必须传 999 —— EQuickAccessTab 里 Decky 是 999，
   * 而 0~8 分别是通知 / 远程同乐 / 语音 / 好友 / 设置……。以前传的 1 在新版
   * SteamOS 上是「远程同乐」，所以回来时压根不在插件页。
   */
  const restoreQam = () => {
    try {
      Navigation.OpenQuickAccessMenu(999);
    } catch {
      Navigation.OpenQuickAccessMenu();
    }
  };

  /**
   * 发一轮对话。grab=true 时连画面一起发；hideQam=true 时先收起 QAM 再抓帧
   * （否则截到的是「QAM 半屏 + 游戏半屏」）。
   * brief 是给界面看的一句短语，代替完整提示词留在你的那条气泡里。
   */
  const runTurn = async (text: string, grab: boolean, hideQam = false, brief = "") => {
    if (busyRef.current) return;
    setError("");
    setActivity("");
    // 用户那条由后端广播回来（带来源、带 ts），这里不抢先插一条，
    // 否则同一句话会先本地显示一次、再被所有端同步的消息覆盖
    curRef.current = "";
    setStreaming("");
    setShot("");
    setBusyBoth(true);

    try {
      // 每次都重新问一次：面板重挂载后本地 ticket 是空的，靠这次拿回来
      const st = await refreshStatus();
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");
      if (!st.unlocked || !st.ticket) throw new Error("插件未解锁，先输入访问令牌");
      const tk = st.ticket;

      let shotPath: string | null = null;
      let game: { appid?: string; name?: string } | null = null;
      if (grab) {
        // 先收面板：CloseSideMenus 走的是 Steam 的 MenuStore，等于替用户按了
        // 一次「关闭侧栏」（按 B 只是回上一级，面板还开着）。
        // 这一下要等的东西不止收起动画：侧栏滑走后 Steam 还要重绘一帧游戏画面，
        // 而 pipewiresrc 抓的是合成结果，抢在前面就会把正在消失的侧栏、甚至侧栏
        // 切走时闪过的通知页一起拍进来。所以这里放宽到 1.2s，后端还会再等一次。
        if (hideQam) Navigation.CloseSideMenus();
        if (hideQam) await new Promise((r) => setTimeout(r, 1200));
        // 只拿落盘路径：一张图的 base64 有 16MB，走 Decky 的 IPC 会让调用失败。
        // 面板预览用插件另存的小缩略图（几十 KB），原图只在插件进程内部读
        const snap = (await call<
          [string, boolean],
          { ok: boolean; shot?: string; thumb?: string; error?: string; game?: { appid?: string; name?: string } }
        >("screenshot", tk, hideQam)) as {
          ok: boolean;
          shot?: string;
          thumb?: string;
          error?: string;
          game?: { appid?: string; name?: string };
        };
        // 抓完立刻把面板连同预览一起摆出来：后面还有几十秒生成时间，
        // 用户应该能在这段时间里确认「发给 AI 的到底是哪一帧」
        if (hideQam) restoreQam();
        if (snap.ok && snap.shot) {
          shotPath = snap.shot;
          setShot(snap.thumb ? `data:image/jpeg;base64,${snap.thumb}` : "");
        } else {
          // 截图失败不阻断提问：这一轮退回纯文字，并在界面上说清楚原因
          setError(`截图失败，这轮只发文字：${snap.error ?? "未知原因"}`);
        }
        game = snap.game ?? null;
      }

      // 游戏名从 Steam 侧拿到就直接写进问题里，免得模型看画面认错游戏、
      // 搜回来一堆别的游戏的攻略
      let body = text;
      const gname = game?.name ?? "";
      if (gname) {
        const aid = game?.appid ? `（appid ${game.appid}）` : "";
        body = `当前游戏：《${gname}》${aid}。\n${text}`;
      }

      const res = (await call<[string, string | null, string, string], { ok: boolean; error?: string }>(
        "start_turn",
        body,
        shotPath,
        brief,
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

  /** 读「截屏询问」模板：和网页设置页共用同一份后端配置 */
  const loadAskTpl = async (tk: string) => {
    try {
      const r = (await call<[string], { ok: boolean; text?: string }>(
        "get_ask_template",
        tk
      )) as { ok: boolean; text?: string };
      if (r.ok && r.text) {
        setAskTpl(r.text);
        setAskTplEdit(r.text);
      }
    } catch {
      /* 取不到就用后端默认模板，这里不打扰用户 */
    }
  };

  /** 保存「截屏询问」模板：清空即恢复默认（后端负责判定） */
  const saveAskTpl = async () => {
    try {
      const r = (await call<[string, string], { ok: boolean; text?: string; error?: string }>(
        "set_ask_template",
        askTplEdit,
        ticket
      )) as { ok: boolean; text?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setAskTpl(r.text ?? "");
      setAskTplEdit(r.text ?? "");
      setError("");
    } catch (e: any) {
      setError(`提示词保存失败：${String(e?.message ?? e)}`);
    }
  };

  /**
   * 截屏询问：收面板 → 抓一帧当前画面 → 带图自动问 AI。
   * 问什么由设置里的模板决定（默认就是「看我卡在哪一步…不要剧透」）；
   * 正在玩哪款游戏由后端从 Steam 的进程/日志里读出来，拼在模板前面，
   * 模型搜攻略时就不会搜到同系列的别的作品。
   * 界面上只留一句短配文：完整提示词有一百多字，长显会把整个面板撑开。
   */
  const shotAsk = async () => {
    if (busyRef.current) return;
    await runTurn(askTpl || DEFAULT_ASK_TPL, true, true, SHOT_ASK_BRIEF);
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
    // 中断多半发生在「面板收着、等抓帧」的这段时间，不把面板拉回来用户会以为插件死了
    restoreQam();
  };

  /** 新会话：后端会向所有端广播 history，这边的对话列表随事件重建 */
  const newSession = async () => {
    if (newing) return;
    setNewing(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; error?: string }>(
        "session_new",
        ticket
      )) as { ok: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受");
      // 不依赖广播的到达时机：本地先把列表和临时状态清干净，
      // 否则按下的瞬间下面还挂着上一段对话，看起来像「没清掉」
      setItems([]);
      setShot("");
      setStreaming("");
      setActivity("");
      curRef.current = "";
    } catch (e: any) {
      setError(`新建会话失败：${String(e?.message ?? e)}`);
    } finally {
      setNewing(false);
    }
  };

  // 未解锁：面板只留解锁框，提问一律不显示，避免用户点了才发现没权限
  if (status && !status.unlocked) {
    return (
      <PanelSection>
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

  // 探测失败时把原因留在面板上：只写「Unknown」用户没法判断是插件没起来、
  // 后端没起来，还是 Decky 那边调用直接抛了异常
  const coreLine = status
    ? status.core_ok
      ? "女仆核心 正常"
      : "异常"
    : probing
      ? "女仆核心 未知"
      : "未连上";
  // 错误码单独占第二行：状态行只放结论，出错时才在下一行写原因，位置不串
  const coreWhy = status && !status.core_ok ? status.core_reason : "";

  return (
    <PanelSection>
      <PanelSectionRow>
        <div style={{ fontSize: 10, lineHeight: 1.5 }}>
          <div style={{ color: DIM }}>
            <span style={{ color: status?.core_ok ? DIM : ERR_COLOR }}>{coreLine}</span>
            {" · "}
            <span>{status ? `${status.ip}:${status.port}` : "—"}</span>
          </div>
          {coreWhy && <div style={{ color: ERR_COLOR }}>{coreWhy}</div>}
          {busy && activity && <div style={{ color: DIM }}>{activity}</div>}
        </div>
      </PanelSectionRow>

      {/* 放在按钮上面：抓帧那一分钟面板是收着的，回来第一眼就该看见进度和取消。
          按钮下多留一点：它和下面的对话区之间原本只隔 row 的默认间距，
          第一句话几乎贴着按钮，看着像挤在一起 */}
      <PanelSectionRow>
        <div style={{ paddingBottom: 6 }}>
          <ButtonItem layout="below" onClick={busy ? cancel : shotAsk}>
            {busy ? `中断（${elapsed}s）` : "截屏询问"}
          </ButtonItem>
        </div>
      </PanelSectionRow>

      {/* 后端没起来时把「启动」放在最显眼的位置：不改什么设置就能用 */}
      {status && !status.core_ok && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={startBackend} disabled={starting}>
            {starting ? "正在启动后端…" : "启动后端"}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {error && (
        <PanelSectionRow>
          <div style={{ ...CARD, color: ERR_COLOR, background: "rgba(224,108,117,.1)" }}>
            {error}
          </div>
        </PanelSectionRow>
      )}

      {shot && (
        <PanelSectionRow>
          {/* 抓到的画面直接摆出来：抓错帧、抓到黑屏、抓到菜单，一眼就能看出来 */}
          <div style={{ ...CARD, padding: 0, overflow: "hidden", border: "1px solid rgba(255,255,255,.12)" }}>
            <img
              src={shot}
              alt="本轮截到的画面"
              style={{ display: "block", width: "100%", height: "auto" }}
            />
            <div style={{ padding: "4px 8px", fontSize: 10, color: DIM }}>本轮发给 AI 的画面</div>
          </div>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <Focusable
          ref={scrollRef as any}
          style={{ maxHeight: 330, overflowY: "auto", display: "block", padding: THREAD_PAD }}
        >
          {streaming && (
            <div style={{ display: "flex", justifyContent: "flex-start", marginBottom: 4 }}>
              <div
                style={{
                  ...CARD,
                  margin: "0",
                  padding: "5px 6px",
                  maxWidth: "100%",
                  minWidth: 0,
                  background: "rgba(255,255,255,.06)",
                  borderLeft: "2px solid #5aa9e6",
                }}
              >
                <Md text={streaming} />
                <span style={{ color: DIM }}>▍</span>
              </div>
            </div>
          )}
          {/* 倒序：最新一条在最上面，进来不用先把列表拉到底。
              正在生成的那条（streaming）排在最前，正好是「最新」的位置 */}
          {items.slice().reverse().map((m, i) => {
            // 一左一右：AI 在左、我说话在右，各带一条同色竖条，
            // 扫一眼就知道哪句是谁说的
            const mine = m.role === "user";
            return (
              <div
                key={`${m.ts}-${i}`}
                style={{
                  display: "flex",
                  justifyContent: mine ? "flex-end" : "flex-start",
                  marginBottom: 4,
                }}
              >
                <div
                  style={{
                    ...CARD,
                    // 气泡吃满整行宽度、左右内边距压到最小：QAM 只有 ~300px，
                    // 每省下一点横向留白，一行就能多塞几个字
                    margin: "0",
                    padding: "5px 6px",
                    // 去掉 82% 的宽度上限：那点留白换来的「左右区分」远不如多显示的字值钱
                    maxWidth: "100%",
                    minWidth: 0,
                    background: mine ? "rgba(90,169,230,.14)" : "rgba(255,255,255,.06)",
                    borderLeft: mine ? "2px solid transparent" : "2px solid #5aa9e6",
                    borderRight: mine ? "2px solid #5aa9e6" : "2px solid transparent",
                    textAlign: mine ? "right" : "left",
                  }}
                >
                  {m.image && (
                    // 这一轮附带的画面：以前只有模型看得到，屏幕上一直是空的
                    <img
                      src={m.image}
                      style={{
                        display: "block",
                        width: "100%",
                        height: "auto",
                        marginBottom: 4,
                        borderRadius: 4,
                      }}
                    />
                  )}
                  <Md text={m.text} align={mine ? "right" : "left"} />
                </div>
              </div>
            );
          })}
          {!items.length && !streaming && (
            <div style={{ ...CARD, color: DIM, textAlign: "center" }}>
              聊天框空空如也，随便问点什么吧
            </div>
          )}
        </Focusable>
      </PanelSectionRow>

      {/* 隔断：上面是可滚动的对话区，下面是输入区。没有这条线时，
          消息滚到底会和「问问 Meido...」粘成一片，看不出哪块能滚。
          上下各留 12px：紧贴着回复和输入框都显得挤 */}
      <div
        style={{
          height: 1,
          background: "rgba(255,255,255,.12)",
          margin: "12px 0 12px",
        }}
      />

      {/* 输入框单独一行 */}
      <PanelSectionRow>
        <TextField
          value={input}
          placeholder="问问 Meido..."
          onChange={(e) => setInput(e.target.value)}
        />
      </PanelSectionRow>

      {/* 发送单独一行：与输入框之间留一点距离 */}
      <PanelSectionRow>
        <div
          role="button"
          onClick={busy ? cancel : ask}
          style={{ width: "100%", marginTop: 6, ...FAKE_BTN }}
        >
          {busy ? "中断" : "发送"}
        </div>
      </PanelSectionRow>

      {/* 隔断：输入区（输入框 + 发送）与下面的操作按钮之间 */}
      <div
        style={{ height: 1, background: "rgba(255,255,255,.12)", margin: "12px 0 12px" }}
      />

      {/* 新会话单独一行 */}
      {status?.core_ok && !busy && (
        <PanelSectionRow>
          <div
            role="button"
            onClick={newing ? undefined : newSession}
            style={{ width: "100%", ...FAKE_BTN, opacity: newing ? 0.5 : 1 }}
          >
            {newing ? "正在新建…" : "新会话"}
          </div>
        </PanelSectionRow>
      )}

      {/* 隔断：新会话与设置之间 */}
      <div
        style={{ height: 1, background: "rgba(255,255,255,.12)", margin: "12px 0 12px" }}
      />

      {/* 设置单独一行 */}
      <PanelSectionRow>
        <div
          role="button"
          onClick={() => setShowSettings((v) => !v)}
          style={{ width: "100%", ...FAKE_BTN }}
        >
          设置
        </div>
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
            {/* 「截屏询问」的提示词：默认就是「看我卡在哪一步…不要剧透，200 字以内」。
                放在这里可以直接改，和网页设置页是同一份；清空保存即恢复默认 */}
            <div style={{ fontSize: 10, color: DIM, marginBottom: 4 }}>
              截屏询问的提示词{status?.game_name ? `（当前游戏：${status.game_name}）` : ""}
            </div>
            <TextField
              value={askTplEdit}
              onChange={(e) => setAskTplEdit(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ display: "flex", gap: 8, width: "100%" }}>
              <div role="button" onClick={saveAskTpl} style={{ flex: "1 1 0", ...FAKE_BTN }}>
                保存提示词
              </div>
              <div
                role="button"
                onClick={() => setAskTplEdit(DEFAULT_ASK_TPL)}
                style={{ flex: "0 0 auto", ...FAKE_BTN }}
              >
                恢复默认
              </div>
            </div>
          </PanelSectionRow>
          </>
      )}

      {/* 底部版本号：主程序版本在前，插件自己的在后。
          两边版本号要求一致（都是 package.json / VERSION 里的那一版），
          不一致时会一眼看出来，省得怀疑装错了包 */}
      <PanelSectionRow>
        <div style={{ fontSize: 10, color: DIM, textAlign: "center" }}>
          核心 {status?.version || "—"} · 插件 {PLUGIN_VERSION}
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}

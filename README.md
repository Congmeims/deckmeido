# DeckMeido

<p align="center">
  <img src="assets/logo.png" width="180" alt="DeckMeido">
</p>

<p align="center">
  <b>住在 Steam Deck 里的 AI 女仆助手</b><br>
  会聊天、会查攻略、会打开游戏、会替你点屏幕——真机上的一切都交给她。
</p>

<p align="center">
  <a href="#功能">功能</a> ·
  <a href="#实现逻辑">实现逻辑</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#配置">配置</a>
</p>

---

## 这是什么

DeckMeido 是一个跑在 Steam Deck 本机上的 AI 代理。它不是"另一个聊天网页"——它能**真正操作这台掌机**：启动 Chrome 打开网页、在输入框里打字、点击按钮、截图确认结果、检查 Steam 库、扫描模拟器 ROM、执行 shell 命令，甚至改名改配置文件。

界面是一套 Web 控制台（FastAPI + 原生 JS），并用 PySide6 封装成原生客户端，可以最小化到系统托盘。同一套界面，两种打开方式，功能完全一致。

名字里的 *Meido* 是日语「女仆」的读音。人格可以在配置里随意改，默认设定是礼貌周全、称你为"主人"、办事干练的日系女仆。

## 功能

### 对话与上下文

- **流式回复 + 思维链展示**：模型思考和回答都实时流出，逐步呈现
- **多会话管理**：新建 / 切换 / 删除，每个会话独立保存历史；新建会话会一并清空上下文用量
- **自动上下文压缩**：占用接近模型窗口上限（默认 95%）时，把旧对话摘要成一段 system 消息，保留最近几轮原文，不中断长任务
- **上下文徽标**：顶栏实时显示 `已用 tokens / 窗口大小 / 百分比`，窗口大小按模型名自动推断（Gemini 1M、Claude 200k、DeepSeek 128k……），也可在配置里写死
- **任意 OpenAI 兼容后端**：GPT、Claude（中转）、DeepSeek、Qwen、GLM、Kimi、LM Studio 本地模型都能接
- **历史持久化**：助手回复连同工具事件时间线一起落盘，刷新网页不会丢失操作记录

### 设备控制

| 能力 | 说明 |
| --- | --- |
| 打开与操作桌面 | 聚焦窗口、按屏幕坐标点击、移动光标、输入文本 |
| 截图 | 游戏模式（gamescope）走 PipeWire 抓帧；桌面模式 spectacle 优先，失败退回 mss；截图时自动收起遮罩 |
| 执行命令 | bash 命令，超时保护、危险命令识别；启动浏览器等常驻程序会自动判定并立即返回，不阻塞 |
| Steam 库 | 列出已安装游戏、商店搜索、通过客户端下载安装（危险操作，强制确认） |
| 模拟器 | 扫描 EmuDeck 标准目录 `~/Emulation/roms`，按机型统计 |
| 系统 | SteamOS 更新检查、系统信息 |
| 文件 | 目录浏览、按行读取、整体写入、精确替换（改代码优先用 `edit_file`） |
| 网络 | 联网搜索、天气查询（联网搜索默认安装，`ddgs` 未装时会返回报错而不是静默失败） |

### 交互细节

- **操作遮罩**：只在会动屏幕的工具执行期间显示半透明遮罩（点击、打字、滚动、拉起可见窗口），随时可点击终止；跑命令、翻文件这类纯后台动作不遮挡视线；注入鼠标/键盘的那几百毫秒内自动透明穿透，避免点击被遮罩吃掉；截图时自动收起
- **权限三档**（作用于 `run_command`）：`manual` 每条命令都审批 / `auto` 只读自动、命中危险特征才确认 / `full` 完全放行。`write_file` / `edit_file` 不受此开关约束，任何模式下都直接落盘；`install_game` 与 `self_update upgrade` 无论模式如何都强制确认
- **语音播报**：调用 Deck 内置 TTS，在掌机扬声器上念出回复
- **图片上传**：拖拽或点击上传，服务端按类型分类存储
- **分享为图片**：多选任意回合，勾选后生成长图，自动剔除思维链
- **深浅双主题**：所有组件（含代码块、滚动条、遮罩）都跟着主题切换，浅色模式下不会出现"黑块"
- **原生客户端**：1.2 倍缩放适配 800p 屏幕，最小化到托盘，单击托盘图标恢复

## 实现逻辑

### 整体架构

```
┌──────────────────────┐        WebSocket        ┌────────────────────────┐
│  网页控制台 / 客户端   │ ◄──────────────────────► │  FastAPI :18765        │
│  (index.html)        │        REST + Token     │  server/main.py        │
└──────────────────────┘                         └───────────┬────────────┘
                                                             │
                              ┌──────────────────────────────┼──────────────────────────┐
                              ▼                              ▼                          ▼
                     ┌────────────────┐          ┌────────────────────┐       ┌──────────────────┐
                     │ LLM 客户端      │          │  工具层 tools.py    │       │  遮罩 overlay.py  │
                     │ 流式 + 思维链   │          │  function calling   │       │  PySide6 半透明窗 │
                     └────────────────┘          └─────────┬──────────┘       └──────────────────┘
                                                           │
                                        ┌──────────────────┼─────────────────┐
                                        ▼                  ▼                 ▼
                                   ydotool / kdotool  Steam / flatpak    bash / 文件系统
```

### 一次对话是怎么跑起来的

1. 前端通过 WebSocket 发出用户消息，服务端把消息追加进当前会话的 `history`
2. 请求前检查上下文占用：若 `used / window ≥ compact_ratio`，先把较早的对话交给模型摘要，替换掉旧消息
3. 调用 LLM（OpenAI 兼容 `/chat/completions`），携带 `tools` 定义和 `stream_options.include_usage`，让服务商在流末尾回报真实 token 用量
4. 流式返回时，前端把 `reasoning_content` 渲染成"思考中"折叠块，把 `content` 逐字渲染进气泡
5. 模型若要求调用工具，服务端按权限模式判断是否放行；放行后执行，把结果作为 `role: tool` 消息回灌，最多连续 `max_tool_rounds` 轮
6. 执行工具期间向前端广播 `tool_start` / `tool_done`，前端渲染成带状态的时间线
7. 全部完成后，助手消息连同 `events`（工具时间线）和 `ts` 一起写入 `server/sessions/<id>.json`

### 几个关键设计

**遮罩只盖会动屏幕的那一步。** `tools.touches_screen()` 判断这次调用会不会真的动屏幕：`desktop` 的 `click` / `move` / `type` / `focus`，`run_command` 里的 `xdotool` / `ydotool` / `kdotool` / `wtype` / `wmctrl` / 启动器类命令，都算；`ls`、`rm`、`spectacle` 截图、`web_search` 不算。`run_agent` 在真正 `execute()` 前后 `set_overlay(True/False)`，所以「清空回收站」这类命令不会再盖住全屏。遮罩的真正用途是给用户一个「随时点一下终止」的入口，而 agent 操作桌面时用户的手没法去按终止；注入鼠标的那几百毫秒里遮罩会切成透明穿透（`set_overlay_passthrough`），否则点击会打在遮罩上被当成「用户终止」。

**截图和遮罩的协同。** 遮罩会盖住整个桌面。截图前，后端把 `lift` 状态置为 `true`，遮罩进程以 25ms 间隔快速轮询，看到后立刻隐藏并回报 ack；后端最多等 200ms 确认它真的收起了，然后才按快门，拍完马上恢复。同时工具描述里写明"截图会自动收起遮罩，不需要等待"，避免模型自己加 `sleep`。

**启动常驻程序不阻塞。** `flatpak run com.google.Chrome` 这类命令的进程不会退出，早期实现会一直等到 60s 超时。现在识别启动器类命令（flatpak / snap / steam / chrome / firefox / mpv / xdg-open 等，且无管道、分号、重定向），最多等 3 秒：命令自行退出就返回真实输出，仍在运行就判定为常驻程序，放它继续跑并立刻返回"已在后台启动"。

**思维链可见。** 多数服务商只有在请求里带 `reasoning_effort` 时才返回 `reasoning_content`。配置默认 `high`，前端在 `thought_end` 之后不自动折叠，思考内容可以一直回看。

**上下文压缩而非丢弃。** 到阈值时不是简单砍掉老消息，而是让模型写一份摘要（保留目标、偏好、已完成操作、待办、关键路径），用一条 system 消息顶替前面的对话，再拼上最近几轮原文。长任务不会因为跨度大而"失忆"。

**Web 与客户端同源。** 客户端不复制界面，直接用 QWebEngineView 加载 `http://127.0.0.1:18765/?native=1`。改一处 HTML，两端同步更新；`native=1` 只用来把"点开图片"这类行为切到系统默认程序。

## 快速开始

### 环境

- Steam Deck（SteamOS 桌面模式）或其他 Linux 桌面
- Python 3.11+（用到 `tomllib`）
- 可选：`spectacle`（KWin 截图，一般自带）、`kdotool` / `ydotool`（按窗口名聚焦与合成输入）、`espeak-ng`（语音播报）

> 控制类插件（`ydotool` / `kdotool` / `espeak-ng`）是「替你在桌面上动手」的核心能力，装它们需要 root：`ydotool` 负责模拟鼠标点击与键盘输入，`kdotool` 按窗口标题聚焦窗口，`espeak-ng` 把回复念出来。`install.sh` 在结尾会把每个控件的用途列出来并**推荐安装**，询问一次（输入 `y` 才走 `sudo`；无终端时自动跳过，不会卡住）。想跳过询问直接装：
>
> ```bash
> bash install.sh install --deps
> ```
>
> 用 `pacman` 装 `espeak-ng` / `ydotool`，用 `paru` 或 `yay` 装 AUR 里的 `kdotool`；装完会写入并启用 `ydotoold` 的 `systemd --user` 服务，开机自动运行（点击报 socket 错误时执行 `systemctl --user restart ydotool`）。缺这些不影响对话、截图、执行命令，只是「模拟鼠标点击 / 键盘输入」和语音播报用不了。

### 一键安装

```bash
git clone https://github.com/Congmeims/deckmeido.git && cd deckmeido && bash install.sh
```

脚本会：建虚拟环境 → 装依赖（含 PySide6）→ 生成配置 → 清理旧的 `deckmeido.service` 与占用端口的残留后端 → 装好应用菜单与桌面快捷方式 → 自动启动客户端（后端由客户端拉起），最后打印带令牌的访问地址。

**不要用 `sudo` / root 执行。** 程序装到当前用户家目录（默认 `~/deckmeido`），用 root 装会落到 `/root` 下，桌面会话里起不来。脚本检测到 root 会直接退出。

不想用 git 的话，直接下载 Release 里的包。资产文件名带版本号，所以链接里必须写具体版本号，换版本就要改文件名（`releases/latest/download/...` 里同样得写带版本的文件名，写错会 404）：

```bash
curl -fLO https://github.com/Congmeims/deckmeido/releases/download/v0.2.0/deckmeido-0.2.0.tar.gz
tar -xzf deckmeido-0.2.0.tar.gz && cd deckmeido-0.2.0 && bash install.sh
```

也可以用同一页面上的 `SHA256SUMS` 先核对包是否完整：

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

包里的 `RELEASE-NOTES.md` 是这一版相对上一版改了什么。

```bash
bash install.sh upgrade      # 升级：同步代码 + 依赖，保留配置与会话
bash install.sh check        # 自检：逐个实测控制类控件是否真的能用
bash ~/deckmeido/uninstall.sh        # 卸载：先取消正在跑的任务，停服务、删快捷方式，保留配置与工作区
bash ~/deckmeido/uninstall.sh --purge   # 连配置与工作区一起删
```

安装脚本会在程序目录留一个 `uninstall.sh`，不用记 `install.sh` 的路径和参数。它只是转调 `install.sh uninstall [--purge]`，逻辑只有一份；只有在 `install.sh` 已经被删掉时，它才走自带的兜底分支。等价的原始写法是 `bash install.sh uninstall [--purge]`。

### 版本与升级

设置页底部显示当前版本号（安装时由 `install.sh` 写入安装目录的 `VERSION`），旁边有「检查更新」：

- **检查更新**：向 GitHub 查询最新 release，比当前版本新就提示，并出现「立即升级」
- **立即升级**：先把本地改动 `git stash`，再 `git fetch --tags` + `git checkout <tag>` 切到新版，然后后台执行 `install.sh upgrade`（同步代码、装依赖、重启后端）。任何一步失败都会自动回滚到升级前的 commit，并把回滚命令写进 `update.log`。配置、会话、工作区都会保留

升级前会先关掉客户端再停后端，避免旧客户端在升级过程中把旧后端拉回来；升级后新进程的 `/api/version` 报出新版本号才算成功。

通过 release tar 包安装的目录没有 `.git`，上面的自动升级用不了，升级按钮会提示不适用：重新下载新包覆盖安装即可（`bash install.sh` 会清理新版本里已不存在的旧文件，配置、会话、工作区不受影响）。也可以在终端里手动升级：

```bash
cd ~/deckmeido && git fetch --tags && git checkout v0.2.0 && bash install.sh upgrade
```

自定义位置与端口：`DECKMEIDO_DIR=~/apps/deckmeido DECKMEIDO_PORT=18765 bash install.sh`

默认不安装 systemd 开机自启：后端由客户端（桌面快捷方式）拉起，退出时一并收走，避免两份后端抢同一端口导致历史不一致。需要后端常驻时再手动生成单元：

```bash
bash install.sh service
systemctl --user daemon-reload && systemctl --user enable --now deckmeido.service
```

### 第一次使用

1. 打开 `http://<deck-ip>:18765/?token=meido`（安装脚本会把完整地址打出来；默认令牌 `meido`）
2. 首次连接会自动弹出设置页——填 **API 地址** 与 **Key**，保存即可开始对话；不配也能进，只是对话不可用
3. 设置页里可以「随机生成」或自定义访问令牌（4-32 位字母数字，校验不分大小写，保存后立即生效）
4. 想用手机连？点设置页里的「二维码」，手机扫码直接带令牌进入
5. 设置页底部能看到版本号，点「检查更新」可直接从网页升级

不跑脚本时的手动启动方式：

```bash
.venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port 18765
.venv/bin/python client/main.py     # 原生客户端
```

## 配置

| 段落 | 键 | 说明 |
| --- | --- | --- |
| `[server]` | `port` / `token` | 监听端口；访问令牌，默认 `meido`，留空则自动生成 |
| `[llm]` | `base_url` / `api_key` / `model` | OpenAI 兼容接口 |
| | `reasoning_effort` | `low` / `medium` / `high`，留空则不发送 |
| | `timeout` / `max_output_tokens` | 请求超时秒数；单次输出上限，`0` 表示用满上下文剩余空间 |
| | `max_continuations` | 撞到输出上限时自动续写轮数，默认 3 |
| `[agent]` | `max_tool_rounds` | 单轮最多连续调用工具次数，默认 `0` = 不限 |
| | `context_window` | 窗口大小，`0` 表示按模型名推断 |
| | `compact_ratio` | 触发压缩的占用比例，默认 0.95 |
| `[power]` | `keep_awake` | 执行命令或显示遮罩时阻止息屏与休眠，默认 `true`（游戏模式不干预） |
| `[persona]` | `name` / `traits` / `extra` | 人格名称与性格描述，改这里就换人格 |
| `[tts]` | `enable` / `engine` / `voice` | 语音播报开关（默认 `false`）与引擎、语言 |
| | `speed` / `max_chars` | 语速（词/分钟，默认 170）；单次播报最长字符数（默认 300） |
| `[security]` | `permission_mode` | `manual` / `auto` / `full` |
| `[workspace]` | `dir` | 编程与文件任务的默认工作区，留空则用 `~/deckmeido_workspace` |

配置文件位置：安装目录下的 `server/config.toml`（首次启动自动生成）。

## 目录结构

```
deckmeido/
├── install.sh                 一键安装 / 升级 / 卸载
├── VERSION                    安装时写入的版本号（设置页与升级检查读它）
├── uninstall.sh               安装时生成的卸载入口
├── requirements.txt           Python 依赖
├── assets/
│   ├── logo.png               应用图标（桌面快捷方式、托盘、遮罩、网页 favicon）
│   └── icon-src/              图标 SVG 源文件
├── client/main.py             PySide6 原生客户端（含托盘、单实例）
├── deploy/
│   ├── deckmeido.service      旧版 systemd 单元（现在默认不安装，仅供手动启用）
│   └── DeckMeido.desktop      桌面快捷方式模板
├── server/
│   ├── main.py                FastAPI 服务、会话、approval、遮罩调度
│   ├── llm.py                 OpenAI 兼容流式客户端
│   ├── tools.py               工具定义与执行实现
│   ├── overlay.py             半透明操作遮罩（PySide6）
│   ├── tts.py                 espeak-ng 语音播报
│   ├── power.py               任务期间抑制息屏与休眠
│   ├── depcheck.py            系统控件自检（ydotool / kdotool / espeak-ng 等）
│   ├── singleton.py           单实例锁
│   ├── updater.py             查最新 release、git pull 与后台升级
│   ├── config.example.toml    配置模板
│   └── static/
│       ├── index.html         单文件 Web 控制台
│       ├── logo.png           鉴权页大图标
│       ├── logo-mark.png      侧栏品牌图
│       └── favicon.png        浏览器标签页图标
└── tools/
    ├── make_icon.py           由 SVG 渲染 PNG 图标
    ├── make_icon_png.py       由成品 PNG 生成各尺寸图标
    └── make_release.sh        打 Release 用的 tar.gz
```

`VERSION` 与 `uninstall.sh` 由 `install.sh` 在安装时生成，不在仓库里。

## 已知限制

- `desktop` 的文字输入中英文都支持：优先走 klipper 剪贴板（`org.kde.klipper.setClipboardContents` + `Ctrl+V`），klipper 不可用时才退回 `ydotool type`，此时只支持英文、数字与符号；窗口聚焦依赖 `kdotool`
- `write_file` / `edit_file` / `list_dir` 的路径只做 `expanduser` + `resolve`，没有工作区边界校验，也能写到工作区外；相对路径相对于后端进程的 cwd（`~/deckmeido`），不是工作区。文件写入也不受 `permission_mode` 约束
- 部分 Wayland 原生窗口无法直接截取，桌面模式需依赖 `spectacle`
- 联网搜索依赖 `ddgs`，已列入 `requirements.txt` 默认安装；环境里没有它时搜索工具会直接返回「No module named 'ddgs'」，不会静默降级
- 会话历史保存在本机 `server/sessions/`，不含云端同步

## 待办

以下为已确认要改、尚未动手的事项，按方向分组，顺序不代表优先级。

### 安装与首次使用

- ~~**初始配置向导**~~ — 已做：Web 面板首次拿到 status 时若 `llm_configured` 为 false 就直接弹出设置页（原先用 `fetch('/api/config')` 并发判断，经常抢在 status 之前拿不到结果而错过引导，已改成等 status 决定）。后端未配置时的回复也改成引导文案
- ~~**`install.sh` 界面重做**~~ — 已做：figlet `Big` 字体的大字标题 + 框线分区 + 依赖表格 + 完成卡片
- ~~**`install.sh` 的 `?` 符号**~~ — 已做：改为 `[?]`
- ~~**Release 内置离线依赖包**~~ — 已做：`tools/fetch_offline_deps.sh` 把依赖树下载到 `deploy/deps/`，`make_release.sh` 打进发布包，`install.sh` 优先 `pacman -U` 本地包、装不上或实测仍缺再回退联网
  - SteamOS 的 A/B 镜像切换会让 pacman 缓存失效，所以离线包独立存放在 `deploy/deps/`，不复用 `/var/cache/pacman/pkg`
  - 离线包不进仓库（`.gitignore`），发版前在 Deck 上重跑一次 `fetch_offline_deps.sh`，否则包会比 holo 仓库旧

### 工具能力

- **`desktop` 补齐手势** — `action` 目前只有 `focus` / `click` / `move` / `type` / `where`，滚动与拖拽只能靠 `run_command` 手拼 `ydotool`，慢且易错。目标：新增 `scroll`（dx/dy）与 `drag`（起止点、中间插值），把常见点击 / 拖动 / 滚动做成预设
- **模拟 Steam Deck 触摸板** — 掌机触摸板是双向滑动 + 轻点，和鼠标滚轮不是一回事。目标：能模拟触摸板 swipe 与双指滚动，供游戏模式或需要精细纵向滚动的场景使用

### 界面与交互

- ~~**遮罩改按工具判定**~~ — 已做：`tools.touches_screen()` 判断这一步会不会真的动屏幕（`desktop` 注入、`xdotool` / `ydotool` / `kdotool` / `wtype` 一类命令、拉起可见窗口），只在执行这类工具的前后 `set_overlay(True/False)`；`handle_chat` 里整轮常显的遮罩已去掉，跑后台命令、翻文件不再挡视线
- ~~**手机端上下文用量徽章**~~ — 已做：顶栏徽章在 ≤560px 隐藏，同一份用量由侧栏状态卡的 `#ctxRow` 显示（85% 起变黄、95% 变红，与徽章同阈值）
- ~~**文件下载改气泡卡片**~~ — 已做：模型输出的 `[名字](/files/<相对工作区路径>)` 在 `renderMd` 里渲染成附件卡片（图标 + 文件名 + 大小），右侧独立下载按钮；新增 `/files/{path}`（限工作区内，带 token 校验）与 `POST /api/file_stat`（补大小）

### 实时画面

- **网页端实时画面（MJPEG 推流）** — 网页端以 30fps 或可变帧率查看设备实时画面，用于遥控时确认状态
  - 路线：游戏模式复用现有 gamescope PipeWire 节点，`pipewiresrc ! videorate ! videoscale ! jpegenc ! multipartmux ! fdsink` 常驻管线，前端 `<img src="/api/stream?token=…">` 走浏览器原生 MJPEG；桌面模式只能走 portal `ScreenCast`（`CreateSession` / `SelectSources` / `Start` / `OpenPipeWireRemote` 接口齐全，但首次需用户在弹出的系统对话框里授权）
  - 实测数据：`gst-launch-1.0` 的 `pipewiresrc` / `jpegenc` / `multipartmux` 三个插件齐备；1280×800 q70 约 16KB/帧（PIL 编码 129fps，CPU 有余量），30fps ≈ 0.5MB/s，960×600 q60 ≈ 0.3MB/s
  - 已排除：`mss` 抓 30 帧仅 0.12s 但 Wayland 原生 gamescope 表面全黑；`spectacle` 单次 806ms，约 1.2fps
  - 已知坑：遮罩是独立 Layer Shell 窗口，游戏模式截不到但桌面模式会混入画面，推流期间需自动 `lift` 或由前端叠加；持续 30fps 编码在掌机上有耗电与发热代价，默认按需（静止降帧 / 帧差触发）；MJPEG 流必须挂 `check_token`，否则等于新开一个免密看屏入口
  - 待定：分辨率与画质档位、是否只在对话框打开时推流、要不要另做 WebRTC 备选（延迟更低但实现重）

### 游戏模式

- **远程控制** — 让局域网设备通过 HTTP 注入按键与触摸，实现「远程下一句指令，agent 代为操作」。路线已确认：`ydotool`（键鼠，`ydotoold` 已在跑）+ `/dev/uinput`（手柄，`deck` 已在 `input` 组且设备可写）；decky-flow 的 EIS 桥是本机 Unix socket 而非网桥，这一层可以省掉
  - 前置：**先做网络鉴权**。`:18765` 目前无 token 监听 `0.0.0.0`，等于把「可执行命令」的入口开放给整个局域网
  - 待实测：切到 gamescope 后后端（user 级 systemd）是否被重建、`ydotoold` 是否存活
  - 待补：`ydotool` 造不出 uinput gamepad（ABXY / 摇杆 / 扳机），需另写虚拟手柄
  - 定位：LLM 推理一来一回 1~3 秒，不适合动作游戏，适合「帮我跑日常 / 挂机刷素材」
- **Decky 插件联动查攻略** — 游戏里按一下「查攻略」：截当前画面 → 交给 DeckMeido 推理 → 结果回到 QAM 面板或手机端
  - 载体：独立 Decky 插件。`plugin_loader.service` 是 `multi-user.target` + `User=root`，切 gamescope 不会停，是游戏模式下唯一可靠的常驻入口；DeckMeido 后端是 user 级，切会话大概率断
  - 三块能力：游戏内截帧 / QAM 入口 + 热键 / 调后端推理并回显
  - 待拍板：攻略来源（内置库 / 现搜 / 先搜后总结）、截帧触发（手动 / 自动识别）、结果呈现位置、是否注册全局热键（会与 Steam 输入争抢）
  - 已知风险：gamescope 下 `kdotool` 全废；PipeWire 截帧只在桌面模式验证过，游戏模式需实测

### 依赖

- **`ddgs`** — 已在 `requirements.txt` 解注释并列为默认安装（`1c598a0`），但本机 `.venv` 尚未安装，`web_search` 仍返回 `No module named 'ddgs'`

---

<p align="center"><sub>DeckMeido · 为你守着这台掌机的女仆</sub></p>

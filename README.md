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
| 网络 | 联网搜索、天气查询（联网搜索需 `pip install ddgs`） |

### 交互细节

- **操作遮罩**：真正控制设备时才在屏幕上显示半透明遮罩，可点击终止；联网搜索、截图这类只读操作不弹
- **权限三档**：`manual` 每条命令都审批 / `auto` 只读自动、危险操作需确认 / `full` 完全放行
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

**遮罩只在必要时出现。** 早先所有工具调用都会弹遮罩，联网搜索时也糊一层，纯属干扰。现在只有真正会改动屏幕内容的操作才显示：`desktop` 的 `click` / `move` / `type`（`focus` 只切换窗口，不弹），以及 `run_command` 中被识别为输入类的命令；截图是只读采集，而且遮罩会被拍进截图里，所以不弹。

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

不想用 git 的话，直接下载 Release 里的包。链接里写 `latest` 就会取最新版，换成具体版本号（如 `deckmeido-0.2.0.tar.gz`）则可以装指定版本：

```bash
curl -fLO https://github.com/Congmeims/deckmeido/releases/latest/download/deckmeido-0.2.0.tar.gz
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

- `desktop` 工具的文字输入依赖 `ydotool type`，只支持英文、数字与符号，中文请改用命令或文件；窗口聚焦依赖 `kdotool`
- 部分 Wayland 原生窗口无法直接截取，桌面模式需依赖 `spectacle`
- 联网搜索依赖 `ddgs`，`requirements.txt` 里默认注释掉了，需要时手动 `.venv/bin/pip install ddgs`
- 会话历史保存在本机 `server/sessions/`，不含云端同步

---

<p align="center"><sub>DeckMeido · 为你守着这台掌机的女仆</sub></p>

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
  <a href="#版本与升级">版本与升级</a> ·
  <a href="#配置">配置</a> ·
  <a href="#待办">待办</a>
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
- **长期记忆（可教学）**：跨会话保留「关于主人」的条目，分用户画像 / 环境经验 / 知识条目三类。对话里说「记住…」她自己存；纠正她的做法（「不对，应该…」）也会存；设置页可以逐条查看、新增、改、删、清空

### 设备控制

| 能力 | 说明 |
| --- | --- |
| 打开与操作桌面 | 聚焦窗口、按屏幕坐标点击、移动光标、输入文本 |
| 游戏模式 | Decky 插件（QAM 面板）里直接对话、看流式回答、虚拟手柄按键；截帧走 gamescope PipeWire 节点 |
| 截图 | 游戏模式（gamescope）走 PipeWire 抓帧；桌面模式 spectacle 优先，失败退回 mss；截图时自动收起遮罩；抓帧以 deck 用户身份降权执行（`XDG_RUNTIME_DIR` 必须是 `/run/user/1000`） |
| 执行命令 | bash 命令，超时保护、危险命令识别；启动浏览器等常驻程序会自动判定并立即返回，不阻塞 |
| Steam 库 | 列出已安装游戏、商店搜索、通过客户端下载安装（危险操作，强制确认） |
| 模拟器 | 扫描 EmuDeck 标准目录 `~/Emulation/roms`，按机型统计 |
| 系统 | SteamOS 更新检查、系统信息 |
| 文件 | 目录浏览、按行读取、整体写入、精确替换（改代码优先用 `edit_file`） |
| 网络 | 联网搜索、天气查询（联网搜索默认安装，`ddgs` 未装时会返回报错而不是静默失败） |

### 交互细节

- **操作遮罩**：只在会动屏幕的工具执行期间显示半透明遮罩（点击、打字、滚动、拉起可见窗口），随时可点击终止；跑命令、翻文件这类纯后台动作不遮挡视线；注入鼠标/键盘的那几百毫秒内自动透明穿透，避免点击被遮罩吃掉；截图时自动收起
- **音量键强制停止**：遮罩期间独占监听音量加/减键，按一下即终止当前操作，不用等画面卡住再找鼠标点。只在遮罩显示时 `EVIOCGRAB` 抢占设备，遮罩消失立刻放开，平时不影响调音量
- **发起来源标注**：同一条对话里每条用户消息都带来源角标——`本端` / `网页` / `手机` / `客户端` / `QAM`。手机浏览器按 UA 自动识别，无需手动设置；没标来源的老消息不显示角标
- **控件自检三态**：绿灯可用 / 红灯故障 / 灰灯「当前环境不适用」（如游戏模式下没有 KWin，`kdotool` 不再误报红灯）。灯色只统计真正需要工作的控件，游戏模式下显示为 `x/5` 而非 `x/6`
- **权限三档**（作用于 `run_command`）：`manual` 每条命令都审批 / `auto` 只读自动、命中危险特征才确认 / `full` 完全放行。`write_file` / `edit_file` 不受此开关约束，任何模式下都直接落盘；`install_game` 与 `self_update upgrade` 无论模式如何都强制确认
- **语音播报**：调用 Deck 内置 TTS，在掌机扬声器上念出回复；网页设置页与 QAM 面板的开关同步（`POST /api/tts` 热更新，不用重启）
- **实时画面**：游戏模式下网页端可边下边播当前画面（gamescope 抓帧 → H.264 硬编 → 分片 MP4），面板可拖动缩放
- **图片上传**：拖拽或点击上传，服务端按类型分类存储
- **文件下载卡片**：回复里的工作区文件路径渲染成带大小与下载按钮的卡片（`/files/<相对路径>`，带令牌校验，只能下载工作区内的文件）
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

**思维链可见。** 多数服务商只有在请求里带 `reasoning_effort` 时才返回 `reasoning_content`。配置默认 `high`，前端在 `thought_end` 之后不自动折叠，思考内容可以一直回看。DeepSeek 的思考默认就开着，同一个 `reasoning_effort` 它只认 `high` / `max`（`low`、`medium` 会被它映射成 `high`）。

**上下文压缩而非丢弃。** 到阈值时不是简单砍掉老消息，而是让模型写一份摘要（保留目标、偏好、已完成操作、待办、关键路径），用一条 system 消息顶替前面的对话，再拼上最近几轮原文。长任务不会因为跨度大而"失忆"。

**DeepSeek 前缀缓存友好。** 默认后端就是 DeepSeek，几处细节专门为它的"硬盘缓存"（命中部分按更低的单价计费）让步：历史只追加、压缩只推进游标（发出去的前缀每轮都一样）、工具结果统一以 `[工具名]` 开头、历史里的旧画面换成一段固定文案不再重发 base64、请求带固定 `user_id` 让这台 Deck 的历史始终落在同一个 KVCache 分桶里。命中情况会打在日志里，鼠标悬停顶栏的上下文徽标也能看到命中率和距上次请求的闲置时长——命中率掉下来通常就是前缀被打断了（换了人设、切了会话、放太久缓存过期）。

**长期记忆是显式教学，不是后台自学。** 只有两种来源：模型主动调 `remember` 工具（对话里明确说「记住」或纠正做法时），以及设置页里手动写。没有后台定时总结、没有把整段对话自动抽成记忆——那样长任务跑几轮就会把上下文塞满噪声。条目存在 `server/memory.json`，去重按「忽略空白与标点」比较，超过 120 条时按「最近被用到 + 最近创建」淘汰。

**记忆段拼在 system prompt 最末尾。** 这是为 DeepSeek 前缀缓存让路：如果记忆插在规则中间，增删一条就会让后面的全部前缀失效，下一轮整段重算。放在最后，改动只影响尾部。每轮对话都重新读一次，所以刚说完「记住」这一轮之后就能生效。

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

GitHub 连不上（或太慢）时走国内镜像站，装的是同一份代码：

```bash
# 有 install.sh 时（比如已经从别处拿到脚本）
bash install.sh from-mirror

# 什么都没有时：先取一份脚本，再让它去镜像站拉最新版
curl -fsSL -o /tmp/dm.sh https://meido.meagames.cn/deckmeido/install.sh
bash /tmp/dm.sh from-mirror
```

镜像站也可以直接下包手动安装（地址里带版本号，换版本要改文件名）：

```bash
curl -fLO https://meido.meagames.cn/deckmeido/deckmeido-0.2.0.tar.gz
tar -xzf deckmeido-0.2.0.tar.gz && cd deckmeido-0.2.0 && bash install.sh
```

脚本会：建虚拟环境 → 装依赖（含 PySide6，pip 自动挑最快的国内源，装失败自动换源）→ 生成配置 → 清理旧的 `deckmeido.service` 与占用端口的残留后端 → 装好应用菜单与桌面快捷方式 → 自动启动客户端（后端由客户端拉起），最后打印带令牌的访问地址。

**不要用 `sudo` / root 执行。** 程序装到当前用户家目录（默认 `~/deckmeido`），用 root 装会落到 `/root` 下，桌面会话里起不来。脚本检测到 root 会直接退出。

GitHub Release 里也有同样的包，文件名带版本号，所以链接里必须写具体版本号：

```bash
curl -fLO https://github.com/Congmeims/deckmeido/releases/download/v0.2.0/deckmeido-0.2.0.tar.gz
tar -xzf deckmeido-0.2.0.tar.gz && cd deckmeido-0.2.0 && bash install.sh
```

也可以用同一页面上的 `SHA256SUMS` 先核对包是否完整：

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

包里的 `RELEASE-NOTES.md` 是这一版相对上一版改了什么。

Release 包里还带 `deploy/deps/` 离线依赖包（`pacman -U` 本地装，装不上再回退联网），同目录的 `install.sh` 会优先用它。离线包不进仓库，发版前在 Deck 上跑一次 `tools/fetch_offline_deps.sh` 刷新。

### 常用命令

```bash
bash install.sh upgrade             # 升级：同步代码 + 依赖，保留配置与会话
bash install.sh upgrade --deps      # 升级后顺带检查并安装控制类插件
bash install.sh from-mirror         # 从镜像站拉最新版覆盖安装
bash install.sh check               # 自检：逐个实测控制类控件是否真的能用
bash install.sh install --deps      # 安装时直接装控制类插件，不再询问
bash ~/deckmeido/uninstall.sh           # 卸载：先取消正在跑的任务，停服务、删快捷方式，保留配置与工作区
bash ~/deckmeido/uninstall.sh --purge   # 连配置与工作区一起删
```

安装脚本会在程序目录留一个 `uninstall.sh`，不用记 `install.sh` 的路径和参数。它只是转调 `install.sh uninstall [--purge]`，逻辑只有一份；只有在 `install.sh` 已经被删掉时，它才走自带的兜底分支。等价的原始写法是 `bash install.sh uninstall [--purge]`。

### 版本与升级

设置页底部显示当前版本号（安装时由 `install.sh` 写入安装目录的 `VERSION`），旁边有「检查更新」：

- **更新源**：默认「国内镜像优先，镜像不通时用 GitHub」，也可以选只用其中一个。镜像站是纯静态目录，升级时下载 `deckmeido-<版本>.tar.gz` 覆盖文件并核对 sha256，**不需要 git**
- **检查更新**：向当前源查询最新版本，比当前版本新就提示，并出现「立即升级」
- **立即升级**：镜像源先打包一份 `update-backup-*.tar.gz`，再下载新版覆盖并核对 sha256，中途出错整个盖回去，成功后删掉备份；GitHub 源则先把本地改动 `git stash`，再 `git fetch --tags` + `git checkout <tag>`，失败回滚到升级前的 commit。之后后台执行 `install.sh upgrade`（同步代码、装依赖、重启后端）。当前源失败会自动切到另一个源，两个都失败才报错。全过程写进 `update.log`，配置、会话、工作区都会保留

升级前会先关掉客户端再停后端，避免旧客户端在升级过程中把旧后端拉回来；升级后新进程的 `/api/version` 报出新版本号才算成功。

不管装的时候用 git 还是 tar 包，上面两条升级都能用。也可以在终端里手动升级：

```bash
bash ~/deckmeido/install.sh from-mirror     # 从镜像站拉最新版覆盖
# 或者 git 安装时切到指定版本
cd ~/deckmeido && git fetch --tags && git checkout v0.2.0 && bash install.sh upgrade
```

### 发布到镜像站

`bash tools/make_release.sh v0.2.0` 会产出 `dist/` 下的 tarball、`.sha256`、`SHA256SUMS`、`latest.json` 和 `install.sh`，按原样 rsync 到镜像目录即可（客户端读 `latest.json` 判断有没有新版）：

```bash
bash tools/make_release.sh v0.2.0
rsync -av dist/latest.json dist/SHA256SUMS dist/install.sh dist/deckmeido-0.2.0.tar.gz* \
      user@meido.meagames.cn:/var/www/deckmeido/
```

GitHub Release 照旧单独发一份，两边版本号保持一致，用户无论走哪个源都能升到同一版。

镜像目录就是一个普通静态目录，nginx 最简配置（纯 GET，不需要任何后端）：

```nginx
location /deckmeido/ {
    alias /var/www/deckmeido/;
    autoindex off;
    sendfile on;
    tcp_nopush on;
    limit_conn addr 2;          # 单 IP 最多 2 条并发，避免一个人占满带宽
    add_header Cache-Control "no-cache";   # latest.json 必须能立刻看到新版
}

# latest.json / .sha256 都是几 KB，但会被反复拉，短缓存减少请求
location ~* /deckmeido/.*\.(json|sha256)$ {
    alias /var/www/deckmeido/;   # 实际按需改成 alias + try_files
    add_header Cache-Control "max-age=60";
}
```

`latest.json` 不能被 CDN 长时间缓存（`max-age` 超过几分钟），否则客户端会一直以为没有新版。

带宽只影响下载时长，不影响正确性：代码包只有几 MB，500KB/s 下单个客户端约十几秒；PySide6 等 pip 依赖走的是国内 pip 源，不经过镜像站。**不要把 `deploy/deps/` 的 pacman 离线包传到镜像站**——那是几十上百 MB，会把这台小带宽机器直接压满，留在 GitHub Release 里就好。

自定义位置与端口：`DECKMEIDO_DIR=~/apps/deckmeido DECKMEIDO_PORT=18765 bash install.sh`
换镜像地址：`DECKMEIDO_MIRROR=https://example.com/deckmeido bash install.sh`（会写进配置，之后升级也走它）

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
client/launch.sh                    # 原生客户端（带守护，崩溃后自动重启）
```

`launch.sh` 会反复拉起 `client/main.py`，并把 C 层崩溃栈写进 `server/client-crash.log`（`faulthandler`）。排查问题时用 `CLIENT_CRASH_RELAUNCH=0 client/launch.sh` 关掉自动重启。

## 配置

| 段落 | 键 | 说明 |
| --- | --- | --- |
| `[server]` | `port` / `token` | 监听端口；访问令牌，默认 `meido`，留空则自动生成 |
| `[llm]` | `base_url` / `api_key` / `model` | OpenAI 兼容接口；默认 DeepSeek（`https://api.deepseek.com` + `deepseek-chat`，地址不用带 `/v1`） |
| | `reasoning_effort` | `high` / `max`，留空则走后端默认（DeepSeek 默认开思考）。OpenAI 系同样发送 `reasoning_effort` |
| | `timeout` / `max_output_tokens` | 请求超时秒数；单次输出上限，`0` 表示用满上下文剩余空间 |
| | `max_continuations` | 撞到输出上限时自动续写轮数，默认 3 |
| `[agent]` | `max_tool_rounds` | 单轮最多连续调用工具次数，默认 `0` = 不限 |
| | `context_window` | 窗口大小，`0` 表示按模型名推断 |
| | `compact_ratio` | 触发压缩的占用比例，默认 0.95 |
| | `compact_target_left` | 压缩后保留最近原文的比例，默认 0.3 |
| | `ask_template` | 「截屏询问」的提示词模板，可在网页「设置」里改 |
| `[power]` | `keep_awake` | 执行命令或显示遮罩时阻止息屏与休眠，默认 `true`（游戏模式不干预） |
| `[update]` | `source` | 更新源：`mirror` / `github` / `mirror-only` / `github-only`，设置页「关于」可改 |
| | `mirror` / `github_repo` | 镜像站目录（默认 `https://meido.meagames.cn/deckmeido`）；GitHub 仓库 |
| `[tts]` | `enable` / `engine` / `voice` | 语音播报开关（默认 `false`）与引擎、语言 |
| | `speed` / `max_chars` | 语速（词/分钟，默认 170）；单次播报最长字符数（默认 300） |
| `[security]` | `permission_mode` | `manual` / `auto` / `full` |
| `[workspace]` | `dir` | 编程与文件任务的默认工作区，留空则用 `~/deckmeido_workspace` |

上表这些项都能在网页「设置」里改，保存即时生效，一般不需要手改文件。配置文件位置：安装目录下的 `server/config.toml`（首次启动自动生成）。

人设与长期记忆不在 `config.toml` 里，各自单独成文件，放在同一目录（`server/`）下：

| 文件 | 内容 | 说明 |
|---|---|---|
| `meidosoul.json` | 人设（女仆的灵魂） | 名字、性格、称呼、语气、禁用词。网页「设置 → 人设」写入，改完即时生效 |
| `memory.json` | 长期记忆 | 用户画像 / 环境经验 / 知识条目三类，网页「设置 → 记忆」或对话里教学写入 |

网页「设置 → 记忆 → 备份」可以把这两个文件的内容导出成一个 `meidosoul.json`，换机器或重装后再导入即可。旧版本的 `config.toml` 里如果有 `[persona]` 段，首次启动会自动搬到 `meidosoul.json`。

`~/.config/deckmeido/` 是另一回事，只放安装脚本自己的状态（`.deps-build-id`、`.deps-offline`），不含配置。

## 目录结构

```
deckmeido/
├── install.sh                 一键安装 / 升级 / 卸载 / 镜像安装 / 自检
├── VERSION                    安装时写入的版本号（设置页与升级检查读它）
├── uninstall.sh               安装时生成的卸载入口
├── requirements.txt           Python 依赖
├── assets/
│   ├── logo.png               应用图标（桌面快捷方式、托盘、遮罩、网页 favicon）
│   └── icon-src/              图标 SVG 源文件
├── client/main.py             PySide6 原生客户端（含托盘、单实例）
├── client/launch.sh           客户端守护脚本（崩溃/切模式后自动重启）
├── plugin/                    Decky 插件（QAM 面板：对话、截屏询问、虚拟手柄）
│   ├── deploy.sh              检测 Decky 与控制用户插件目录
│   ├── main.py                插件后端（鉴权、抓帧、手柄、攻略流程）
│   └── src/index.tsx          QAM 面板前端
├── deploy/
│   ├── deckmeido.service      旧版 systemd 单元（现在默认不安装，仅供手动启用）
│   ├── DeckMeido.desktop      桌面快捷方式模板
│   └── deps/                  离线 pacman 包（不进仓库，发版前用工具脚本刷新）
├── server/
│   ├── main.py                FastAPI 服务、agent 循环、approval、遮罩调度
│   ├── session_store.py       会话状态与落盘（store 单例、原子写、压缩游标）
│   ├── llm.py                 OpenAI 兼容流式客户端
│   ├── memory.py              长期记忆存储（memory.json：三类条目、去重、淘汰）
│   ├── persona.py             人设存储（meidosoul.json：名字、称呼、语气、禁用词）
│   ├── tools.py               工具定义与执行实现
│   ├── overlay.py             半透明操作遮罩（PySide6）
│   ├── stream.py              实时画面（gst 管线 + 分片 MP4）
│   ├── tts.py                 espeak-ng 语音播报
│   ├── power.py               任务期间抑制息屏与休眠
│   ├── depcheck.py            系统控件自检（ydotool / kdotool / espeak-ng 等）
│   ├── singleton.py           单实例锁
│   ├── updater.py             查最新版本（镜像 / GitHub 双源）、覆盖或 git 升级
│   ├── config.example.toml    配置模板
│   └── static/
│       ├── index.html         单文件 Web 控制台
│       ├── logo.png           鉴权页大图标
│       ├── logo-mark.png      侧栏品牌图
│       └── favicon.png        浏览器标签页图标
└── tools/
    ├── make_icon.py           由 SVG 渲染 PNG 图标
    ├── make_icon_png.py       由成品 PNG 生成各尺寸图标
    ├── fetch_offline_deps.sh  把 pacman 依赖树下到 deploy/deps/（发版前在 Deck 上跑）
    ├── make_release.sh        打 Release 包，同时产出 latest.json 供镜像站用
    └── ui-preview.html        QAM 面板的浏览器预览
```

`VERSION` 与 `uninstall.sh` 由 `install.sh` 在安装时生成，不在仓库里；`deploy/deps/` 已被 `.gitignore` 忽略。

## 已知限制

- `desktop` 的文字输入中英文都支持：优先走 klipper 剪贴板（`org.kde.klipper.setClipboardContents` + `Ctrl+V`），klipper 不可用时才退回 `ydotool type`，此时只支持英文、数字与符号；窗口聚焦依赖 `kdotool`
- `write_file` / `edit_file` / `list_dir` 的路径只做 `expanduser` + `resolve`，没有工作区边界校验，也能写到工作区外；相对路径相对于后端进程的 cwd（`~/deckmeido`），不是工作区。文件写入也不受 `permission_mode` 约束
- 部分 Wayland 原生窗口无法直接截取，桌面模式需依赖 `spectacle`
- 联网搜索依赖 `ddgs`，已列入 `requirements.txt` 默认安装；环境里没有它时搜索工具会直接返回「No module named 'ddgs'」，不会静默降级
- 会话历史保存在本机 `server/sessions/`，不含云端同步

## 待办

只列**尚未完成**的事项。已完成的能力已经写进上面的功能与实现说明，不再在这里留记录。顺序不代表优先级。

### 待真机验证

以下代码已写完，但只在开发机上做过语法与逻辑校验，需要上 Deck 实测：

- **镜像覆盖升级** — 从 `latest.json` 下载 → sha256 → 解压覆盖 → 后台 `install.sh upgrade` 的整条链路，以及中途失败时 `update-backup-*.tar.gz` 的还原能力
- **pip 测速选源** — `pick_pip_index()` 在 Deck 网络下的排序是否合理，某个源超时后能否顺利换到下一个
- **实时画面编码器探测** — `_probe_encoder` 走 `vah264enc` 还是回落 `x264enc`，需要实际跑一次确认（`stream.py`）
- **QAM 面板对话同步** — `ready()` 补发能否覆盖 Decky 重挂监听的时机；助手回复的来源在新旧会话混排时是否标对
- **非 Steam 游戏名** — `compatdata` 目录名在 SteamOS 上是无符号还是带符号写法（已双向兜底）；网页游戏、原生 Linux 程序不走 Proton、没有 `STEAM_COMPAT_DATA_PATH` 时会拿不到名字

### 功能

- **远程控制** — 让局域网设备通过 HTTP 注入按键与触摸，实现「远程下一句指令，agent 代为操作」。路线已确认：`ydotool`（键鼠）+ `/dev/uinput`（手柄）；decky-flow 的 EIS 桥是本机 Unix socket 而非网桥，这一层可以省掉
  - 前置：**先做网络鉴权**。`:18765` 若不设令牌就监听 `0.0.0.0`，等于把「可执行命令」的入口开放给整个局域网
  - 已实测：切到 gamescope 后后端存活、`:18765` 正常监听、`ydotoold` 未断、QAM 的 WebSocket 连通
  - 定位：LLM 推理一来一回 1~3 秒，不适合动作游戏，适合「帮我跑日常 / 挂机刷素材」
- **桌面模式实时画面** — 目前只支持游戏模式（gamescope PipeWire 节点），桌面模式还需走 portal ScreenCast
- **QAM 的全局热键 / 独立截图按钮** — 全局热键会和 Steam 输入争抢，暂缓

### 待真机验证（补充）

上述已知问题的修复改动，同样只在开发机上校验过：

- **客户端守护** — `client/launch.sh` 的 `WAYLAND_DISPLAY` 等待与退避重启节奏，在 Deck 上切游戏模式时是否真的能把窗口拉回来
- **崩溃栈** — `server/client-crash.log` 能否记录到 gamescope 掐连接时的 C 层栈
- **plugin 属主自愈** — Decky 以 root 加载后，`plugin/` 是否已恢复为 deck 所有，`git pull` 是否不再报 `Permission denied`

### 已知问题

暂无。曾经列在这里的四项已处理：

- **切游戏模式时 PySide6 客户端崩溃**（`The Wayland connection broke`）—— Qt 在 C 层直接 abort，Python 层捕获不到。现在由 `client/launch.sh` 守护：异常退出后等 Wayland 回来再拉起，连续崩溃时退避（最长 30 秒）；同时用 `faulthandler` 把崩溃栈落到 `server/client-crash.log`。桌面快捷方式、`install.sh` 的启动与各停止路径均已改为围绕 `launch.sh`
- **journal 每分钟一条 sudo 噪声** —— 根因是自检定期执行 `sudo -n true` 探免密，失败一次就在 journal 留一条。现在**完全不做免密探测**：网页自检只跑不需要 root 的实测，装系统包一律引导到终端执行 `bash install.sh check --fix`，由 `sudo` 自己提示输入密码；`install.sh` 的预热也直接 `sudo -v`，不再预读策略
- **`plugin/` 目录所有权漂移** —— 插件侧新增 `_restore_owner()`，加载时与写票据、开日志前把属主改回 deck；`updater.py` 的 `_fix_owners()` 在 `git` 升级前再兜一次
- **上下文压缩的摘要质量** —— 摘要短于历史长度 1/20（且至少 120 字）时，用更明确的分段要求重试一次，取更长者；两版都不合格就保留原文

---

<p align="center"><sub>DeckMeido · 为你守着这台掌机的女仆</sub></p>

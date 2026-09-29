<div align="center">

<img src="assets/logo.png" alt="Avibe" width="120"/>

# Avibe

### Avibe 是本地优先的 Agent OS——你的 AI 伙伴，住在你自己的机器上。

**所有 Agent，所有订阅，任意屏幕，都在你自己的机器上。**

[![GitHub Stars](https://img.shields.io/github/stars/avibe-bot/avibe?color=ffcb47&labelColor=black&style=flat-square)](https://github.com/avibe-bot/avibe/stargazers)
[![Release](https://img.shields.io/github/v/release/avibe-bot/avibe?labelColor=black&style=flat-square)](https://github.com/avibe-bot/avibe/releases/latest)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?labelColor=black&style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green?labelColor=black&style=flat-square)](LICENSE)

<a href="https://www.producthunt.com/products/vibe-remote?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-vibe-remote" target="_blank" rel="noopener noreferrer"><img alt="Avibe — 本地优先的 Agent OS | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1104967&theme=light&t=1774450119248"></a>

[文档](https://docs.avibe.bot) · [English](README.md) · [中文](README_ZH.md)

**驱动** ![Claude Code](https://img.shields.io/badge/Claude%20Code-D4A27F?style=flat-square&logo=anthropic&logoColor=white) ![OpenCode](https://img.shields.io/badge/OpenCode-00B4D8?style=flat-square) ![Codex](https://img.shields.io/badge/Codex-412991?style=flat-square)

**从这些地方找到它** ![Browser](https://img.shields.io/badge/Browser-111827?style=flat-square&logo=googlechrome&logoColor=white) ![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat-square&logo=slack&logoColor=white) ![Discord](https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white) ![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=flat-square&logo=telegram&logoColor=white) ![WeChat](https://img.shields.io/badge/WeChat-07C160?style=flat-square&logo=wechat&logoColor=white) ![Lark](https://img.shields.io/badge/Lark%20%2F%20Feishu-3370FF?style=flat-square&logo=bytedance&logoColor=white)

</div>

<br/>

<img src="assets/screenshots/v4/workbench-chat-zh.png" alt="Avibe Workbench——在浏览器里和本地 Agent 对话、查看执行过程并直接回应" />

---

## 你的 AI agent 很强——但被困住了

Claude Code、Codex、OpenCode 都很能打。但是：

- 🖥️ **困在一台机器上。** 它活在终端里，合上笔记本它就停了。
- 📵 **够不着。** 离开工位，你连它在干什么都看不到，更别说指挥。
- 💸 **额度总在关键时刻见底。** 一个套餐干到一半撞上限额，另一个却在吃灰；哪个值回票价，你也说不清。
- 🔒 **被锁死。** 每个工具都想当整个栈：它的 app、它的云、它的订阅，你的代码还得传到别人的盒子里。

## Avibe 把这件事反过来

**一条命令，把你自己的机器变成 AI 伙伴的家。** 从浏览器、手机或任意聊天软件驱动*官方*的 Claude Code、Codex、OpenCode；你手上的订阅和 API Key 汇进同一个本地网关。代码、密钥和 agent 进程都留在你的机器上——`avibe.bot` 只负责登录和安全隧道，从不经手你的工作区。

```bash
bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'
```

浏览器自动打开，简短的设置向导会找到你已有的 agent（缺的可以一键装上）、接好模型，然后直接带你进入 Workbench。

> 开源——想看可以先读一遍[安装脚本](https://github.com/avibe-bot/avibe/blob/master/install.sh)。短链只是到这个文件的 307 重定向。

<details>
<summary><b>用 Windows？</b></summary>

Windows 上推荐用 WSL，兼容性最好——见 [从零用 WSL 跑 Avibe](docs/WINDOWS_WSL_ZH.md)。里面讲清楚 WSL 装在哪、用哪个终端、在哪运行安装命令、怎么打开 Web UI。
</details>

> 💚 **Avibe 是用 Avibe 自己做出来的。** 这个项目从头到尾都是我用 Avibe 开发的——从浏览器、从手机指挥 Claude Code、Codex、OpenCode，在不在电脑前都能无缝衔接。越往后做越快，体验和效率直接拉爆。—— [@alex_metacraft](https://x.com/alex_metacraft)

---

## 你能得到什么

### 💬 一个真正的 Workbench，不是又一个 Chat 框

对话、文件、编辑器、终端都在同一个窗口化工作区里，agent 做出来的应用和 Show Page 就开在旁边。浏览器不再是一个设置面板，而是真的像一个操作系统。

<img src="assets/screenshots/v4/apps-library-zh.png" alt="Avibe 多窗口 Workbench，同时打开文件、终端和编辑器" />

### 🔀 Model Hub——所有订阅，一个网关

订阅登录一次、API Key 添加一次——官方厂商、中转站、聚合平台、自建服务都行。Avibe 的本地网关按你定的顺序，把每个 Agent 的模型请求路由过去。

- **一个额度用完，自动换下一个。** 某个来源撞上额度、限流或网络故障，路由里的下一个来源立刻接手；首选来源恢复后，下一轮对话自动切回去。
- **一个池子，所有 agent 共用。** Claude Code、Codex、OpenCode 可以共享同一批来源，每个模型各有自己的路由。
- **钱花得值不值，一眼看清。** 按模型和来源看用量趋势，实时查看 5 小时和每周额度，再按 API 价格折算你的用量——直接告诉你每个套餐已经回本几倍。
- **一键搬家。** 已经在支持的 CLI 里登录过？经你确认后一键迁入。凭据和路由都留在你的机器上。

### 🧠 它有自己的时间线——Agent Harness

大多数 AI 工具只在你打字时才动。Avibe 给 agent 四个持久化基础能力——**运行、定时、监听、查历史**——让它能自己发起工作、等到合适的时机、在后台跑完再回来汇报。定时 shell 命令成功时不打扰你，失败时可以直接交给 Agent 处理。

不用背参数，直接说：

- *"盯着这个 PR，有可执行的 review 意见了再回来找我。"*
- *"每个工作日早上跑一次部署检查，把总结发到这里。"*
- *"为这个故障单独开个调查会话，结论汇报回这个频道。"*
- *"CI 挂了就总结日志；过了就告诉我这个 PR 能不能合。"*

<img src="assets/screenshots/v4/harness-tasks-zh.png" alt="Agent Harness 定时任务，展示调度、Agent、会话和投递详情" />

### 🤖 带上你已经信任的 Agent

运行的是官方 Claude Code、Codex、OpenCode CLI，不是仿制品，统一收进一套 Agent 注册表。每个 Agent 单独选模型和推理强度，把不同项目或频道交给合适的专家。Skill 写一次，Avibe 就用同一种方式把它加载给三个后端。

一个 agent 把活儿派给另一个时，**Runs** 会把协作画成一张图：谁发起了哪个后台会话、结果汇报给谁、每个节点背后的执行历史。

<img src="assets/screenshots/v4/agents-graph-zh.png" alt="Avibe Agent 运行关系图，展示父会话把工作委派给 Claude Code、Codex 和 OpenCode 后台会话" />

### 🎨 Show Pages——一图胜千言

agent 直接交给你一个活的网页——仪表盘、流程图、diff、报告，或者一个小应用。点一个元素、框一块区域、在截图上圈一圈，说一句你想要什么，agent 就会改页面，或者就在你指的地方回答。

页面可以保持私有、只分享给指定的人，或者发布成一个好记的链接。常用的固定到应用栏，打开就是一个 app。

<img src="assets/screenshots/v4/show-page-zh.png" alt="Show Page 评审：评论锚定在页面元素上，Agent 回复在页面上" />

### 📱 手机、聊天软件、任意浏览器

<img src="assets/screenshots/v4/workbench-mobile-zh.png" alt="手机上的 Avibe Workbench" width="270" align="right" />

活儿在你的机器上跑，你不用守在它跟前。想要窗口化操作就用 Workbench，想快就用 Slack、Discord、Telegram、微信或飞书——连到的是同一批 Agent、同一个会话。

- 🔔 **需要你时它会拍拍你。** 把 Workbench 装成手机或桌面 app，任务需要你的那一刻就收到推送。
- 🎙️ **动嘴不动手。** 实时语音输入边说边出字，说完自动就地整理好。桌面上按 ⌥Z（Windows 和 Linux 上是 Alt+Z）。
- 🌍 **你自己的 `you-app.avibe.bot`。** 运行 `vibe remote`，本地 Workbench 就能从任意浏览器访问——不用 VPN，不用端口转发。
- 🔒 **只有你邀请的人能进。** 远程登录只对你授权的邮箱开放，认证、路由、主机校验全部默认拒绝。

你在飞机上、在咖啡馆、用着借来的电脑。agent 提醒你一声，打开链接指挥几句，然后接着去忙。

<br clear="all"/>

### 🔐 Vaults——按名字用密钥，不碰密钥值

API Key 或 token 添加一次。Agent 按名字申请，你在浏览器里批准，Avibe 把它交给需要它的命令、认证请求或签名操作。Vault 的响应永远不包含密钥值；接收密钥的命令仍需避免把它打印出来。passkey 保护托管目前处于预览阶段。

**还有**——thread 即会话，随处可续 · agent 需要你拍板时弹按钮和表单 · 丰富的附件，音视频直接内联播放 · 键盘快捷键 · 后端一键升级。

---

## Avibe 凭什么不同

| | |
|---|---|
| **本地优先，归你所有** | AI 伙伴、它的执行、你的密钥和代码都留在你的机器上。`avibe.bot` 只签发身份、提供安全隧道，从不代理你的工作区。 |
| **所有第一方 agent，一个家** | 驱动*官方*的 Claude Code、Codex、OpenCode。按任务、按项目、按频道切换，不被任何一家锁死。 |
| **所有订阅，协同干活** | Model Hub 把你的套餐和 Key 汇成一个池子，自动故障切换，还告诉你每一份值多少。 |
| **浏览器和聊天，都是一等公民** | Workbench、手机、Slack、Discord、Telegram、微信、飞书——同一个 agent，同一个会话。 |
| **没有中间商** | 你和 agent 之间没有额外的推理循环，token 直接给到你选的 agent。 |

---

## 它怎么工作

```
  你，在任何地方             你的机器                                你的模型
┌──────────────┐      ┌──────────────────────────────────────┐      ┌──────────────┐
│ Browser      │      │ Avibe                                │      │ Subscriptions│
│ Phone app    │ ───▶ │   Claude Code ─┐                     │      │ API keys     │
│ Slack        │      │   Codex       ─┼─▶ Model Hub ────────┼────▶ │ Relays       │
│ Discord …    │ ◀─── │   OpenCode    ─┘                     │      │ Self-hosted  │
└──────────────┘      └──────────────────────────────────────┘      └──────────────┘
```

1. **你开口**——在浏览器或聊天软件里：*"给设置页加个深色模式。"*
2. **Avibe 路由**到对的 Agent、对的项目。
3. **agent** 读你本地的代码、写代码、把过程流式回传；每次调用走哪个来源，由 Model Hub 决定。
4. **你在同一个界面里 review**，在 thread 里迭代，之后在任何地方接着干。

Avibe 通过 Slack Socket Mode、Discord Gateway、Telegram 长轮询、微信轮询或飞书 WebSocket 主动向外连接——聊天控制不需要任何公网入站端口。agent 的 prompt 只发给你配置的模型来源。

---

## Avibe vs OpenClaw

| | Avibe | OpenClaw |
|---|---|---|
| **上手** | 一条命令 + 网页向导，几分钟搞定。 | Gateway + channels + JSON 配置，准备花一个下午。 |
| **安全** | 本地优先，只用 Socket Mode / WebSocket，无公网入站端口，攻击面极小。 | Gateway 要暴露端口，组件更多，面更大。 |
| **Token 成本** | 中间没有额外的推理循环，token 直接给到你选的 agent。 | 每条消息都带着长长的人设/编排上下文，任务还没开始 token 就先烧在开销上。 |
| **锁定** | 驱动官方 agent CLI，自带订阅和 Key，按任务切换。 | 绑死在它自己的助手循环里。 |

OpenClaw 是一个常驻的个人助手。Avibe 是给你已经信任的 agent 用的 **Agent OS**：agent 还是它自己，数据留在本地，而"同事感"来自把 agent 放进你本来就在工作的地方。

---

## 常见问题

<details>
<summary><b>能用我现有的 Claude 或 ChatGPT 订阅吗？</b></summary>

能。Claude Code 和 Codex 可以继续用各自的登录；也可以把支持的订阅作为网关来源加进 Model Hub，获得额度追踪、用量分析和自动接管。各厂商的条款依然适用；如果某种登录方式可能带来账号限制，Avibe 会在登录前提醒你。详见 [Model Hub](https://docs.avibe.bot/zh/concepts/model-hub)。
</details>

<details>
<summary><b>能跑本地模型吗？</b></summary>

这里的"本地优先"指你的**代码、数据和执行**留在你的机器上——不一定是模型权重。如果你也想让推理在本地，Model Hub 可以指向任何兼容的自建服务。
</details>

<details>
<summary><b>我的代码和数据会去哪？</b></summary>

代码、密钥和 agent 进程都留在你的机器上。agent 的 prompt 发给你配置的模型来源。`avibe.bot` 负责登录和远程 Web UI 隧道，但不代理你的工作区；唯一的例外是语音输入，它会经 `avibe.bot` 做转写。每一条网络路径都列在[边界清单](https://docs.avibe.bot/zh/concepts/local-first#哪些东西会离开你的机器)里。
</details>

<details>
<summary><b>Avibe 要付费吗？</b></summary>

Avibe 开源（MIT），免费运行。你自带 agent 订阅或 API Key，直接付给服务商——不加价，也不用再多订一份。
</details>

<details>
<summary><b>支持哪些 agent 和平台？</b></summary>

**Agent：** 官方 Claude Code、Codex、OpenCode CLI。**界面：** 内置浏览器 Workbench（可安装到桌面和手机），以及 Slack、Discord、Telegram、微信、飞书。
</details>

<details>
<summary><b>远程访问安全吗？</b></summary>

`vibe remote` 从你的机器上跑一条 Cloudflare 隧道；浏览器流量必须先登录，而且只有你授权的邮箱能进。认证、路由、主机校验全部 **fail-closed**，聊天控制也不需要任何公网入站端口。
</details>

<details>
<summary><b>Avibe 和 OpenClaw、Hermes 有什么不同？</b></summary>

OpenClaw 和 Hermes 是 *agent*——一个是网关式助手，一个是会自我进化的 agent。Avibe 在另一层：**Agent OS**。它给任何 agent 一个统一的世界模型——Agent、会话、Show Page、Harness、Model Hub——让 agent 能自己排期、搭自己的循环、通过真正的交互层找到你；然后它运行的是你自带的官方 Claude Code、Codex、OpenCode（Avibe 原生 agent [在路线图上](#路线图)）。OpenClaw 的具体对比见上面的[对比表](#avibe-vs-openclaw)。
</details>

---

## 认识云团子（Vibey）

<div align="center">
<img src="assets/mascot/cloud-tuanzi.png" alt="云团子 / Vibey——Avibe 里的那团气体意识" width="200"/>
</div>

住在你的 Workbench 和聊天软件里。读得懂气氛，会接你昨天没做完的活儿。不确定就先问一句，你专注的时候它不打扰，凌晨两点灵感来了就动手，第二天给你留张便条，说改了哪儿。

> Avibe 是 agent 住的那个家，云团子是住在里面的那位同事。

做过什么都有据可查，有自己的脾气。你修了它的 bug，它会道谢。

---

## 命令

```bash
vibe            # 启动 Avibe 并打开 Workbench
vibe status     # 查看服务和配置状态
vibe stop       # 停止本地服务
vibe upgrade    # 升级到最新版本
vibe doctor     # 诊断常见安装问题；"vibe doctor repair" 执行安全修复
vibe remote     # 通过 avibe.bot 从任意设备访问 Workbench
vibe agent      # 运行和管理 Avibe agent
vibe task       # 安排定时工作（cron / 一次性）
vibe watch      # 等一个条件成立，然后行动
vibe runs       # 查看 agent 运行历史
vibe show       # 创建、查看和发布 Show Page
vibe vault      # 管理密钥、请求、认证请求与签名
```

| 在聊天里 | 作用 |
|---|---|
| @ 一下 bot | 开一个任务或提问 |
| 在 thread 里回复 | 继续同一个 agent 会话 |
| `/stop` | 停止当前会话 |

完整参考：[命令](docs/COMMANDS_ZH.md) · [CLI](docs/CLI_ZH.md)

---

## Agent

设置向导会检测你机器上的 Claude Code、Codex 和 OpenCode，缺哪个、你选了哪个，就帮你装上。想自己装的话：

<details>
<summary><b>Claude Code</b></summary>

```bash
npm install -g @anthropic-ai/claude-code
```
</details>

<details>
<summary><b>Codex</b></summary>

```bash
npm install -g @openai/codex
```
</details>

<details>
<summary><b>OpenCode</b></summary>

```bash
curl -fsSL https://opencode.ai/install | bash
```

除非 `~/.config/opencode/opencode.json` 放行工具调用，否则 OpenCode 每次调用工具都会弹审批；设置向导可以帮你写好：

```json
{ "permission": "allow" }
```
</details>

---

## 安全

- **本地优先**——Avibe 跑在你的机器上，代码和 agent 进程都留在那。
- **无公网入站端口**——聊天控制只用 Socket Mode / WebSocket / 长轮询。
- **你的密钥，你的数据**——存放在 `~/.avibe/`，只发给你配置的模型来源。已有安装会保留 `~/.vibe_remote/` 作为兼容路径。
- **远程访问默认拒绝**——`avibe.bot` 只负责身份和隧道，从不经手你的工作区。

---

## 卸载

```bash
vibe stop
avibe_home="${AVIBE_HOME:-$HOME/.avibe}"
avibe_home="${avibe_home/#\~/$HOME}"
uv tool uninstall avibe-os
uv tool uninstall vibe-remote   # 旧版安装
vibe_bin="$(command -v vibe)" && rm -f "$vibe_bin" "$(dirname "$vibe_bin")/.vibe.avibe-generation"
rm -rf "$avibe_home/runtime/install-generations"
rm -rf "$avibe_home" ~/.vibe_remote
```

---

## 路线图

接下来要做的：

- **原生桌面 app**——带签名自动更新的 macOS 和 Windows 版本正在测试中。
- **更深入的交互式工作流**——在 Chat、应用与 Show Page 之间加入更多直接操作、结构化决策和原位协作。
- **保护档 Vault 托管**——完成不可变 sandbox 完整性门禁后，再向真实用户开放 passkey 保护的密钥。
- **SaaS 模式**——一键托管上手 + 云端中继，而执行依然留在你自己的机器上。
- **Avibe 原生 agent**——为这个运行时调校的第一方 agent，与你自带的官方 CLI 并存。

近期已上线：支持自动接管、额度追踪和用量分析的 Model Hub · 实时语音输入 · Show Page 分享 · 三个后端统一的 Skills · 三步设置向导 · Agent Harness 与运行关系图 · 多窗口应用 · 标准档 Vaults。

---

## 文档

- **[官方文档](https://docs.avibe.bot)**——快速上手、概念、平台与 agent 指南、排障
- **[Avibe 是什么](https://docs.avibe.bot/zh/concepts/agent-os)**——Agent OS 模型
- **[Model Hub](https://docs.avibe.bot/zh/concepts/model-hub)**——来源、路由、自动接管、用量与额度
- **[CLI 参考](docs/CLI_ZH.md)** · **[命令](docs/COMMANDS_ZH.md)** · **[Show Pages](docs/SHOW_PAGES.md)**
- **[让 AI agent 帮你装](docs/INSTALL_FOR_AI_ZH.md)**——把它丢给 Claude Code、Codex 或 OpenCode，引导式安装
- **[Slack](docs/SLACK_SETUP_ZH.md)** · **[Discord](docs/DISCORD_SETUP_ZH.md)** · **[Telegram](docs/TELEGRAM_SETUP_ZH.md)** 设置指南

---

<div align="center">

**拥有你的 agent。随处都能找到它。**

[立即安装](#avibe-把这件事反过来) · [文档](https://docs.avibe.bot) · [报 bug](https://github.com/avibe-bot/avibe/issues) · [关注 @alex_metacraft](https://x.com/alex_metacraft)

</div>

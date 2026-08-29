<div align="center">

<img src="assets/logo.png" alt="Avibe" width="120"/>

# Avibe

### Avibe 是本地优先的 Agent OS——你的 AI 伙伴，住在你自己的机器上。

**拥有你的 agent。随处都能找到它。永不被锁死。**

[![GitHub Stars](https://img.shields.io/github/stars/avibe-bot/avibe?color=ffcb47&labelColor=black&style=flat-square)](https://github.com/avibe-bot/avibe/stargazers)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?labelColor=black&style=flat-square)](https://www.python.org/)
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
- 🔒 **被锁死。** 每个工具都想当整个栈：它的 app、它的云、它的订阅，你的代码还得传到别人的盒子里。

## Avibe 把这件事反过来

**一条命令，把你自己的机器变成 AI 伙伴的家。** 你驱动的是*官方*的 Claude Code、Codex、OpenCode——从浏览器或任意聊天软件——而代码与密钥都留在你的机器上，avibe.bot 也看不到你的数据。

```bash
curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch
```

浏览器自动打开，跟着简短向导走完，你的机器就成了一个随处可达的 Agent OS。

> 开源——想看可以先读一遍[安装脚本](https://github.com/avibe-bot/avibe/blob/master/install.sh)。短链只是到这个文件的 307 重定向。

<details>
<summary><b>用 Windows？</b></summary>

Windows 上推荐用 WSL，兼容性最好——见 [从零用 WSL 跑 Avibe](docs/WINDOWS_WSL_ZH.md)。里面讲清楚 WSL 装在哪、用哪个终端、在哪运行安装命令、怎么打开 Web UI。
</details>

> 💚 **Avibe 是用 Avibe 自己做出来的。** 这个项目从头到尾都是我用 Avibe 开发的——从浏览器、从手机指挥 Claude Code、Codex、OpenCode，在不在电脑前都能无缝衔接。越往后做越快，体验和效率直接拉爆。—— [@alex_metacraft](https://x.com/alex_metacraft)

---

## 你能得到什么

### 💬 一个真正的 Workbench，不是又一个 Chat 框

在同一个窗口化工作区里对话、查看文件、编辑代码、运行终端命令。内置应用和 Agent 做出的 Show Page 可以并排打开，让浏览器真正像一个 Agent OS，而不是设置面板。

<img src="assets/screenshots/v4/apps-library-zh.png" alt="Avibe 多窗口 Workbench，同时打开文件、终端和编辑器" />

### 🧠 它有自己的时间线——Agent Harness

大多数 AI 工具只在你打字时才动。Avibe 给 Agent 几个持久化基础能力——**运行、定时、监听、查历史**——让它能发起工作、等到合适的时机、后台跑完回来汇报。任务和 Watch 的详情会展示触发条件、Agent 与会话绑定、投递配置和运行历史；独立定义也可以有意不设置对话投递目标。

<img src="assets/screenshots/v4/harness-tasks-zh.png" alt="Agent Harness 定时任务，展示调度、Agent、会话和投递详情" />

<img src="assets/screenshots/v4/harness-watches-zh.png" alt="Agent Harness Watch 列表与展开的详情面板，展示命令、Agent、会话、投递和跟进消息" />

### 🤖 带上你已经信任的 Agent

在一套 Agent 注册表后面运行官方 Claude Code、Codex 和 OpenCode CLI。为不同 Agent 选择模型和推理强度，把项目路由给合适的专家，同时让可复用的 Skills 跨后端共享。

<img src="assets/screenshots/v4/agents-zh.png" alt="Avibe Agent 注册表，统一管理 Claude Code、Codex 和 OpenCode 后端" />

打开**运行**，就能把协作关系看成一张图：谁启动了每个后台 Session、结果向哪里汇报，以及每个节点背后的执行历史。

<img src="assets/screenshots/v4/agents-graph-zh.png" alt="Avibe Agent 运行图，展示主 Session 向 Claude Code、Codex 和 OpenCode 后台 Session 委派工作" />

### 🎨 Show Pages——指着评论，就地迭代

当一张图胜过一段话，Agent 直接给你一个实时网页——流程图、仪表盘、diff、报告或小应用。你可以对元素或截图原位评论，Agent 也能在你指的位置回答或直接改好页面。

在 Chat 中点击**可视化**，即可把当前 Session 切换到它的 Show Page。将鼠标悬停在按钮上，可以改为在新的应用窗口或浏览器标签页中打开；也可以向下拖拽后松开，在指针位置放置一个新窗口，或拖到**应用**按钮上，把该 Show Page 固定到应用栏。

<img src="assets/screenshots/v4/show-page-zh.png" alt="Show Page 审阅界面，包含原位评论与 Agent 页面内回复" />

### 🔐 Vaults——Vault 响应不暴露密钥值

添加一次**标准档**密钥，在浏览器里处理 Agent 的授权请求。Avibe 的 Vault 响应不会返回密钥值；接收密钥的命令仍然不得自行打印它。需要 passkey 的**保护档**在 sandbox 完整性门禁启用前仍处于上线前阶段。

### 🌐 浏览器和通讯平台都是一等界面

需要窗口和丰富交互时用 Workbench；聊天最快时用 Slack、Discord、Telegram、微信或 Lark / 飞书。它们连接的是同一批 Agent 和会话。

<img src="assets/screenshots/v4/platforms-zh.png" alt="Avibe 通讯平台连接：Slack、Discord、Telegram、微信和 Lark" />

### 📱 装进口袋

<img src="assets/screenshots/v4/workbench-mobile-zh.png" alt="移动端 Avibe Workbench" width="270" align="right" />

机器在干活，你不用守着它。把 Workbench 装成桌面或手机 app，用内置语音转文字交代任务，有活儿需要你时直接收到推送。运行 `vibe remote`，同一个本地 Workbench 通过安全的 `avibe.bot` 隧道，从任意浏览器都能打开——不用 VPN 或端口转发。

你在飞机上、在咖啡馆、用着借来的电脑。agent 提示有个活儿需要你。点开链接，指挥两句，再走开。

- 🌍 **你自己的 `you-app.avibe.bot`**——30 秒登录，slug 跟你一辈子
- 🔒 **每道关卡默认拒绝**——鉴权、路由、host 校验都是 fail-closed
- 📱 **移动端友好**——为借来的小屏设计

**数据面留在你的机器上**；`avibe.bot` 只负责控制面的握手。

<br clear="all"/>

**还有** —— 按频道路由 Agent · 可恢复会话（thread = session）· 交互式提问 · 语音转文字 · 文件附件 · 推送与完成通知。

---

## Avibe 凭什么不同

| | |
|---|---|
| **本地优先，且属于你** | AI 伙伴、它的执行、你的密钥和数据，都留在你的机器上。`avibe.bot` 只签发身份和一条安全隧道，从不中转你的数据。 |
| **一套底座，通吃所有第一方 agent** | 驱动*官方*的 Claude Code、Codex、OpenCode。自带订阅或 key，按任务切换，不被任何一家的 silo 绑死。 |
| **浏览器和聊天，都是一等入口** | 用浏览器 Workbench，或 Slack、Discord、Telegram、微信、飞书。同一个 agent，同一批会话。 |
| **没有中间人** | 你和 agent 之间没有第二层推理。token 直接花在你选的 agent 上。 |

---

## 它怎么工作

```
┌──────────────┐            ┌──────────────┐            ┌──────────────┐
│     你         │   浏览器    │              │   stdio    │  Claude Code  │
│  （任意地点）   │   Slack    │    Avibe      │ ─────────▶ │  OpenCode     │
│               │  Discord   │ （你的机器）   │ ◀───────── │  Codex        │
│               │  Telegram  │              │            │               │
│               │   微信      │              │            │               │
│               │   飞书      │              │            │               │
└──────────────┘            └──────────────┘            └──────────────┘
```

1. **你说一句**——在浏览器或聊天软件里：*“给设置页加个深色模式。”*
2. **Avibe 路由**到你配置的 agent，落在正确的项目里。
3. **agent** 读你本地的代码、写代码、把结果流式发回来。
4. **你审阅**，在同一个界面里迭代，之后随时随地接着干。

**你的代码留在你的机器上。** Avibe 在本地运行，通过 Slack Socket Mode、Discord Gateway、Telegram 长轮询、微信轮询或飞书 WebSocket 向外连接——正常聊天控制不需要任何公网入站端口。Prompt 只会发给你选的 AI 提供商。

---

## Avibe vs OpenClaw

| | Avibe | OpenClaw |
|---|---|---|
| **设置** | 一条命令 + Web 向导，几分钟搞定。 | Gateway + channels + JSON 配置，准备搭一下午。 |
| **安全** | 本地优先，只走 Socket Mode / WebSocket。没有公网入站端口，攻击面小。 | Gateway 暴露端口，组件更多、面更大。 |
| **Token 开销** | 中间没有额外推理 loop，token 直接花在你选的 agent 上。 | 每条消息都背着一长串人设/编排上下文，活儿还没开始 token 先烧。 |
| **锁定** | 驱动官方 agent CLI，自带 key，按任务切换。 | 绑在它自己的助手循环上。 |

OpenClaw 是个 always-on 的个人助手——闲聊很好，干真活儿就贵。Avibe 是给你已经信任的那些 agent 用的**本地优先 Agent OS**：agent 还是它自己，数据留在本地，同事感来自把 agent 放进你本来就在用的工作流里。

---

## 常见问题

<details>
<summary><b>能跑本地模型吗？</b></summary>

这里的「本地优先」指你的**代码、数据、执行**留在你自己的机器上——不是模型权重。Avibe 驱动你配置的 agent（Claude Code、Codex、OpenCode）；如果你也想本地推理，OpenCode 可以指向本地或 OpenAI 兼容端点。
</details>

<details>
<summary><b>我的代码和数据去哪了？</b></summary>

你的代码、密钥、agent 进程都留在你自己的机器上。`avibe.bot` 只签发身份和一条安全隧道——从不代理或存储你的数据。Prompt 只发给你选择的 AI 提供商。
</details>

<details>
<summary><b>用 Avibe 要付费吗？</b></summary>

Avibe 开源（MIT）、免费运行。你自带 agent 订阅或 API key，直接付给你的提供商——没有加价，也没有第二份订阅。
</details>

<details>
<summary><b>支持哪些 agent 和平台？</b></summary>

**Agent：** 官方 Claude Code、Codex、OpenCode CLI。**界面：** 内置浏览器 Workbench，外加 Slack、Discord、Telegram、微信、飞书 / Lark。
</details>

<details>
<summary><b>远程访问安全吗？</b></summary>

`vibe remote` 会开一条 Cloudflare 隧道；浏览器流量在登录之后才能到你的机器。鉴权、路由、host 校验全部 **fail-closed**，常规聊天控制没有任何公网入站端口。
</details>

<details>
<summary><b>Avibe 和 OpenClaw、Hermes 有什么不同？</b></summary>

OpenClaw 和 Hermes 是 *agent*——一个偏网关式助手，一个会自我进化。Avibe 是完全不同的一层：**Agent OS**。它给任何 agent 一套统一的世界模型——agents、sessions、Show Pages、Harness——让它能自己调度、自己搭 loop、通过真正的人机交互层来找你，再去驱动你自带的官方 Claude Code、Codex、OpenCode（Avibe 原生 agent 已在[路线图](#路线图)上）。OpenClaw 的细节见上方的[对比表](#avibe-vs-openclaw)。
</details>

---

## 像同事一样跟它说话

用人话说，Harness 在背后自己组合命令：

- *“盯着这个 PR，有 actionable review 再回来处理。”*
- *“每个工作日上午跑一遍部署检查，把摘要发到这里。”*
- *“为这个 incident 新开一个调查会话，但把结论发回这个频道。”*
- *“CI 挂了就总结日志；过了就告诉我这个 PR 能不能合。”*

**对话中途换 agent**——加个前缀就行：

```
Plan: 给 API 设计一个新的缓存层
```

**按项目路由**——不同的活儿，不同的 agent：

```
frontend   → OpenCode    （快速迭代）
backend    → Claude Code  （复杂逻辑）
prototypes → Codex        （快速试验）
```

---

## 认识云团子（Vibey）

<div align="center">
<img src="assets/mascot/cloud-tuanzi.png" alt="云团子 / Vibey——Avibe 里的那团气体意识" width="200"/>
</div>

住在你的 Workbench 和聊天软件里。读得懂气氛，会接你昨天没做完的活儿。不确定就先问一句，你专注的时候它不打扰，凌晨两点灵感来了就动手，第二天给你留张便条，说改了哪儿。

> Avibe 是 agent 住的那个家，云团子是住在里面的那位同事。

什么都记得，有自己的脾气。你修了它的 bug，它会道谢。

---

## 命令

```bash
vibe            # 启动 Avibe 并打开 Workbench
vibe status     # 查看服务和配置状态
vibe stop       # 停止本地服务
vibe doctor     # 诊断常见安装问题；用 "vibe doctor repair" 显式执行安全修复
vibe remote     # 通过 avibe.bot 从任意设备访问 Workbench
vibe agent      # 运行和管理 Avibe agent
vibe task       # 安排定时工作（cron / 一次性）
vibe watch      # 等一个条件成立，然后行动
vibe runs       # 查看 agent 运行历史
vibe vault      # 管理密钥、请求、认证请求与签名
```

| 在聊天里 | 作用 |
|---|---|
| @ 一下 bot | 开一个任务或提问 |
| 在 thread 里回复 | 继续同一个 agent 会话 |
| `/stop` | 停止当前会话 |

完整参考：[命令](docs/COMMANDS_ZH.md) · [CLI](docs/CLI_ZH.md)

---

## 前置条件

至少装一个 agent：

<details>
<summary><b>OpenCode</b>（推荐）</summary>

```bash
curl -fsSL https://opencode.ai/install | bash
```

在 `~/.config/opencode/opencode.json` 里加上这一段，跳过权限弹窗：

```json
{ "permission": "allow" }
```
</details>

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

---

## 安全

- **本地优先**——Avibe 跑在你的机器上，代码和 agent 进程都留在本地。
- **没有公网入站端口**——聊天控制只走 Socket Mode / WebSocket / 长轮询。
- **你的 key、你的数据**——存在 `~/.avibe/` 下，只发给你选的 AI 提供商。老安装保留 `~/.vibe_remote/` 作为兼容路径。
- **远程访问默认拒绝**——`avibe.bot` 只代理身份和隧道，从不碰你的数据。

---

## 卸载

```bash
vibe stop
uv tool uninstall avibe-os
uv tool uninstall vibe-remote   # 旧版安装
rm -rf ~/.avibe ~/.vibe_remote
```

---

## 路线图

接下来要做的：

- **更深入的交互式工作流** —— 在 Chat、应用与 Show Page 之间加入更多直接操作、结构化决策和原位协作。
- **保护档 Vault 托管** —— 完成不可变 sandbox 完整性门禁后，再向真实用户开放 passkey 保护的密钥。
- **SaaS 模式** —— 一键托管上手 + 云端中继，而执行依然留在你自己的机器上。
- **Avibe 原生 agent** —— 为这个运行时调校的第一方 agent，与你自带的官方 CLI 并存。

近期已上线：多窗口应用、Show Page 标注、Agent Harness、标准档 Vaults、语音转文字与 Skills 管理。

---

## 文档

- **[官方文档](https://docs.avibe.bot)**——快速上手、概念、平台与 agent 指南、排障
- **[Avibe 是什么](https://docs.avibe.bot/zh/concepts/agent-os)**——Agent OS 模型
- **[CLI 参考](docs/CLI_ZH.md)** · **[命令](docs/COMMANDS_ZH.md)**
- **[让 AI agent 帮你装](docs/INSTALL_FOR_AI_ZH.md)**——把它丢给 Claude Code、Codex 或 OpenCode，引导式安装
- **[Slack](docs/SLACK_SETUP_ZH.md)** · **[Discord](docs/DISCORD_SETUP_ZH.md)** · **[Telegram](docs/TELEGRAM_SETUP_ZH.md)** 设置指南

---

<div align="center">

**拥有你的 agent。随处都能找到它。**

[立即安装](#avibe-把这件事反过来) · [文档](https://docs.avibe.bot) · [报 bug](https://github.com/avibe-bot/avibe/issues) · [关注 @alex_metacraft](https://x.com/alex_metacraft)

</div>

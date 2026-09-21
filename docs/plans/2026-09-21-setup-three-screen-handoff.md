# Setup 三屏重构实现清单（开发交接）

状态：待开发实现。Owner 于 2026-09-19 07:06 批准了桌面连接/设置对齐清单（含稳定框弹窗），2026-09-20 至 09-21 又将引导流程定为"三屏"版本（亮点 → 模型网关 → 助手启用），本文档是当前最终状态的统一交接。

## 0. 来源与基线

- 设计稿：`/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen`（Pencil 原生编辑，禁止文本改 .pen）。最近一次原生保存 SHA-256：`3e5469a08e0b78f7a6e2f167b67f21afa657ed0f4c3e1b48ee2b97cf0383fc23`。
- 交互原型（模拟数据）：`https://max-app.avibe.bot/show/ses36vg559de2/`（私有）。原型代码在 `/Users/max/.avibe/show/ses36vg559de2/src/`，其中 `pages/index.tsx` 为主流程、`GatewaySetup.tsx` 为第二屏、`KeyImport.tsx` 为发现提示与导入弹窗、`SetupHandoff.ts` 为转场动画、`setup-flow.css` / `gateway-flow.css` 为样式。原型是行为参考，设计稿是视觉参考。
- 已批准合同：`docs/plans/2026-09-19-desktop-connection-settings-alignment.md`（avibe 仓库 worktree `desktop-alignment-contract-20260919` 内同名文件），其中导入语义、稳定框、Settings 独立页、Workbench 侧栏/首页/Composer 条款继续有效。
- 实现基线：派发时刷新 `origin/master`（合同批准时为 `fd3734a99`），独立 worktree，走正常 PR/评审/CI 门禁。
- 验收环境：owner 指定的 `https://avibe-cloud-e2e-app.avibe.bot`；不重建已退役的本地 Incus 环境。

## 1. 范围总览

三屏引导流程（无侧栏壳）：

1. 第一屏 欢迎/协作亮点：三助手接力演示 + 六个工作入口 + 「立即开始」。
2. 第二屏 添加模型提供商：顶部提供商卡片 → 模型网关 → 底部三个助手的路由图；发现提示胶囊；导入弹窗；添加来源弹窗。
3. 第三屏 选择/启用助手：安装/启用/默认模型路由/升级；「进入工作台」。

相邻已确认改动（同一设计轮，见第 11 节）：Settings 独立页、Workbench 侧栏、首页与 Composer、浅色主题按钮配色。

不在范围：桌面套壳加载页、真实凭据扫描/导入的生产行为变更（复用现有 Model Gateway scan/apply）、Model Gateway 服务语义重构、macOS 菜单栏等 OS 层改动。

## 2. 全局壳、主题与锚点

- 壳：无 Workbench 侧栏。顶栏高 94（紧凑 78 / 移动 82）；左侧品牌 = Avibe  logo 图 + 「Avibe」(22/600) + 「Agent OS」(12, muted)；右侧圆形语言按钮 44×44（Languages 图标），菜单含「简体中文 / English」+ 当前项对勾，外部点击或 Escape 关闭。Setup 页 logo 不加边框。
- 主题令牌：dark 背景 `#080812`、表面 `#11111c`、正文 `#f5f1e8`、次级 `#9ba3b8`、描边 `#ffffff24`、强调 `#5BFFA0`、强调浅底 `#5bffa014`；light 背景 `#f4f6fb`、表面 `#ffffff`、正文 `#0a0a14`、次级 `#5c6079`、描边 `#08081218`、强调 `#10B981`、浅底 `#10b98114`。浅色实心绿按钮文字与图标为白色；深色实心绿按钮文字 `#080812`。
- 卡片 hover：克制绿色描边 + 阴影 `0 2px 16px -4px #5bffa038`（预览最终值，设计稿旧值 blur 12 弃用），无位移/缩放。
- 主列宽：标准桌面 976；大屏（≥1600×950）1200；761–1020 为 `calc(100% - 48px)`；移动 `calc(100% - 40px)` 且 max 460。标题 h1 `clamp(32px,3.4vw,46px)`（大屏 50、紧凑 34、移动 30）；副标题 `clamp(15px,1.35vw,18px)`（大屏 20、紧凑/移动 15/14）。
- 主行动锚点（三屏不变）：CTA 区为三列网格（列宽 = 卡片宽），主按钮占中列第一行，高 56（紧凑 52）、圆角 12、字号 17（紧凑 16）；返回按钮占中列第二行，同宽同高。三屏切换时主按钮坐标与尺寸不变。
- 动效偏好：尊重 `prefers-reduced-motion`；页面隐藏或暂停时停播；移动端切屏回顶部。

## 3. 第一屏 欢迎/协作亮点

- 文案：标题「各有所长，接力完成」/ 副「让 Claude Code、Codex、OpenCode 一起工作」；EN「Different strengths. One team.」/「Bring Claude Code, Codex, and OpenCode together」。
- 三张助手卡：Claude Code（PM）、Codex（开发）、OpenCode（测试）。卡内演示区播放工作字幕（如「撰写需求文档」→「需求文档已就绪」）与骨架内容；电路连线（含连接点圆点）在流程到达时脉冲；当前活跃卡高亮。
- 接力循环：横向流 0.75s、回程 1s，整循环约 8.7s；文字切换 0.25s；到达时对应卡高亮/完成态。
- 回路字幕「规划 · 开发 · 测试 · 交付」：圆角胶囊、90% 透明度、带遮罩底，垂直居中于回程线上（不贴容器底）。
- 六个入口：手机 APP、Slack、Discord、Telegram、飞书、微信；圆形 logo 容器 52（紧凑 48）、名称 12px、间距紧凑；文案「6个工作入口，随时随地安排工作」。手机 APP 项不用绿色高亮；自动轮播 hover 效果（2s 间隔），hover/focus 时暂停。Setup 状态下整块隐藏。
- 主按钮「立即开始」+ 右箭头；转场播放期间禁用。

## 4. 第二屏 添加模型提供商

- 文案：标题「添加模型提供商」/ 副「添加订阅或 API Key，由模型网关统一路由」。
- 顶部三张提供商卡：
  - 槽位 1–2 = 用户本机检测到/已配置的提供商（显示掩码 key）；不足两个时按默认 OpenAI、Anthropic 补齐。
  - 槽位 3 恒为「添加更多」（虚线边框 + plus 图标 + 「订阅 / API Key」），通过它添加成功后右上角显示数量徽标。
  - 卡内三行信息：logo、名称、key 行。key 行文案：已连接 = 「API Key sk••••34d8」样式；检测到未添加 = 「检测到 API Key sk••••xxxx」；移动端隐藏「检测到 API Key」标签只显示掩码（单行省略）。
  - 状态样式：已添加/已选中 = 浅绿底 + 绿描边 + 克制阴影 + 右侧单个对勾；未添加 = 常规描边 + 右侧 plus。对勾位置：桌面右侧垂直居中；移动端右上角（6px 内缩、14px）。不出现「已添加 / Added」文字。
  - 检测到的候选默认自动选中；点击卡片切换选中（aria-pressed），选中 = 纳入本次导入批次。
- 路由图：顶部三卡 →（入线）→ 模型网关 →（出线）→ 底部三助手。连线样式与第一屏电路一致（1.5px 强调色、圆头、端点圆点 r2.5、脉冲动画）；几何按卡片中心实测（resize 时重算）。
- 模型网关卡：宽 = 提供商卡宽，高 72（移动端自适应 min 72）；居中两行文字「模型网关」(18/600) + 「模型一次接入，网关统一路由」(11, muted)；不放 logo；有已连接提供商时进入 connected 强化样式。
- 底部三助手：Claude Code / Codex / OpenCode，logo + 名称（名称与第一、三屏一致）；桌面高 84、移动 120（移动端三行：logo/名称/状态小字，见第 13 节待确认 1）。
- 摘要行（aria-live）：有 pending 选择 = 「已选 N 个提供商：names · 仅复制，保留原配置」；已添加 = 「已添加 N 个提供商：names」带对勾；错误 = 「连接未完成，请检查配置后重试。」；无选择 = 「未选择提供商」。
- 发现提示胶囊（合同条款，设计稿含，当前预览未挂载，实现时恢复）：
  - 居中、内容宽、桌面高 44、1px 描边、无阴影；与上下元素间距 20px；dark `#172F27`/`#30473E`，light `#EDF7F1`/`CEDFD5`（light 描边 `#CEDFD5`）。
  - 文案「发现 {{count}} 个可导入模型网关的 API Key」（count 来自真实候选数）；轻按钮「查看并导入」；帮助链接「什么是模型网关？」；最右 X 关闭。
  - 帮助 tooltip 文案：「模型网关集中管理 API Key 和模型连接，为 AI 助手提供统一的模型入口。导入只会复制你选中的 API Key，原有配置会保留。」支持 hover、键盘 focus、移动端 tap；Escape/外部点击关闭。
  - 关闭不导入、不改就绪状态，保留布局槽位（主按钮不跳动）；后续入口在设置-模型。
  - 移动端两行：第一行图标+全文+X，第二行「查看并导入」+帮助链接；≤360px 再收紧内边距与图标尺寸；英文长文案允许自然换行不截断。
- 导入弹窗「导入 API Key 到模型网关」：
  - 描述：「复制已有 API Key，方便在模型网关统一管理和使用，无需重新填写。仅导入你选中的项，原有配置会保留」。
  - 列表行：checkbox + 提供商 logo + 名称 + 掩码 key + 来源说明（如「Codex · 本机配置」）；默认全选；空选择禁用导入按钮「导入选中的 N 项」。
  - 说明行：「仅导入 API Key，不包含订阅登录凭据。相同来源不会重复导入。」
  - 进度：「正在导入 N 个 API Key…」+「正在将选中的配置复制到模型网关」；完成：「已导入 N 个 API Key」+「原有配置已保留。助手连接状态不变」；完成数为累计值。
  - 有剩余：「还有 N 项未导入，可随时继续」+「继续选择」回选择态；「完成」关闭。
  - 语义：整批原子成功或整批失败+重试（保留选择与输入）；不做部分成功；已导入来源不再作为候选；导入完成不等于助手就绪。复用现有 `/api/models/migration/scan` 与 `/apply` 及能力 gating，不复制 OAuth/订阅凭据。
  - 关闭后焦点回到触发按钮。
- 添加来源弹窗（主按钮无服务时点击 / 「添加更多」进入，标题「添加订阅或 API Key」/ 从添加更多进入为「添加更多模型提供商」）：
  - 方式页签：已检测到（仅当存在未上榜的检测到来源时显示）/ 订阅 / API Key；页签切换不改变弹窗外框（稳定框，见第 7 节）。
  - 已检测到：多选列表（logo、名称、掩码、来源），已添加行禁用并显示「已添加」标记；说明「选择即可添加，无需重新填写；原有配置会保留。」
  - 订阅：仅 OpenAI(ChatGPT) 与 Anthropic(Claude)；说明「前往服务商完成授权，无需在 Avibe 输入账号密码。」
  - API Key：默认八家按序 OpenAI、Anthropic、xAI、Gemini、DeepSeek、Qwen、Kimi、OpenRouter，真实 logo、统一容器与光学尺寸；「更多服务商」折叠内 Mistral、Cohere、Z.AI（含已配置项）；输入框 placeholder `sk-demo-example`，空输入禁用主按钮。
  - 进度两阶段（保存→检查）对应文案「正在保存模拟配置…/正在检查模型连接…」（生产用真实阶段文案）；失败错误框保留输入与选择，主按钮变「重试」。
  - 页脚：取消 / 主按钮（「添加选中的 N 项」/「前往 ChatGPT/Claude 登录」/「添加」）。
- 主按钮文案状态：有 pending 选中 = 「导入 N 项并继续」；失败后 = 「重试导入 N 项并继续」；已有服务 = 「继续，选择 AI 助手」；无服务 = 「添加订阅或 API Key」（点击打开添加弹窗）；busy = 「正在连接…/正在检查连接…」并禁用。
- 返回按钮「返回介绍」（主按钮下方同宽）。
- 首次进入序列动画：顶部三卡 → 入线 → 模型网关 → 出线 → 底部三助手（约 1.1s）；reset 可重播（预览控制，不随产品 shipped）。

## 5. 第三屏 选择/启用助手

- 文案：标题「选择你的 AI 助手」/ 副「启用助手，确认默认模型，即可开始工作」。
- 卡片结构：身份行（logo 30、名称 17/600、角色 11/700 右置、启用 Switch 仅 installed 时显示）→ 分隔线 → 状态 pill（已启用/未启用/未安装/安装中/升级中，圆点或 spinner）→ 描述行 → 底部动作区。
  - 描述行：未安装「安装 X，使用已添加的模型」；已启用「使用模型网关的默认路由」；已安装未启用「启用后即可使用已添加的模型」。
  - 动作：未安装 = 「立即安装」（outline + Download，安装中 spinner「正在安装…」）；已启用且有路由 = 默认模型 chip（「默认模型」小字 + 模型名 + ChevronRight，打开路由弹窗）；已安装未启用 = 「已安装，未启用」静态行（启用走身份行 Switch）；有更新 = 「立即升级」（outline + ArrowUpToLine，可与已启用共存，升级中 spinner）。
  - 已启用卡高亮：绿描边 + 浅绿底 + 克制阴影（与第二屏已添加选中同款）。
- 默认模型路由弹窗「默认模型路由」：描述「所有已启用的助手共用。优先使用首选模型，不可用时按顺序尝试备用模型。」；行 = 提供商 logo + 模型名 + 「服务名 · 首选/备用 N」；上移/下移按钮（首尾禁用）；单路由时说明「当前模型已设为首选，无需额外配置。添加更多来源后可设置备用顺序。」；页脚「添加模型来源」（回第二屏）+「完成」；关闭焦点回触发 chip。
- 主按钮「进入工作台」启用条件：存在 ready 服务且已设默认模型路由，且至少一个助手 installed+enabled；否则禁用。点击后才进入 Workbench 新建会话。
- 返回按钮「返回模型」（主按钮下方同宽）；dark 下 hover 不变背景只变文字对比。
- 全部未安装场景：三卡均「立即安装」，主按钮禁用；用户可先回第二屏补模型来源再安装。
- 状态保留：返回第二屏再进入时，已添加/选中/启用/安装状态全部保留（09-20 修复过回退丢状态 bug）。

## 6. 转场动效

- 1→2（providers handoff）：对第一屏三卡做快照，三卡收缩下移成为第二屏底部三助手位置；顶部三卡与模型网关自上方淡入压入；连线随后绘制。
- 2→3（assistants handoff）：顶部三卡、模型网关与连线退场；底部三卡自下方顶起放大为第三屏卡片尺寸。
- 实现要点：快照 + FLIP 式插值（原型 `SetupHandoff.ts`）；reduced-motion、暂停、页签隐藏时直接切换不播；移动端切换前回顶部；转场期间主按钮禁用且坐标不变。

## 7. 弹窗稳定框（合同条款，适用于第二屏三个弹窗）

- 同一弹窗在页签/状态切换时外框不变：标题、描述、页签、关闭、页脚锚定；中部内容区必要时滚动（长错误、提供商列表、授权说明）。
- 公共框属性参考：宽 568、内边距 24、圆角 16、间距 20；高度取该助手/该弹窗"较长常规表单"的常规高度（例：Claude 对可共用 588），并受视口约束；不同弹窗可有各自公共高度，同一弹窗各状态必须一致。
- 加载、凭据类型切换、授权进度、错误都套用同一外框；切换保留已填草稿与焦点；隐藏面板不保留可聚焦控件、不启动重复授权副作用。
- 移动端考虑键盘与安全区；不把单一绝对高度套用到全应用。
- 验收：同视口反复切换 订阅/API Key（及 API Key/Auth Token 类状态），标题、页签、外框、页脚不动；长内容可滚动、动作可达、输入存活。

## 8. 文案表（ZH / EN，走现有 i18n 资源）

| 位置 | 中文 | English |
| --- | --- | --- |
| 一屏标题/副 | 各有所长，接力完成 / 让 Claude Code、Codex、OpenCode 一起工作 | Different strengths. One team. / Bring Claude Code, Codex, and OpenCode together |
| 一屏按钮 | 立即开始 | Get started |
| 回路字幕 | 规划 · 开发 · 测试 · 交付 | Plan · Build · Test · Deliver |
| 入口文案 | 6个工作入口，随时随地安排工作 | 6 ways to work. Wherever you are. |
| 二屏标题/副 | 添加模型提供商 / 添加订阅或 API Key，由模型网关统一路由 | Add model providers / Add a subscription or API Key. Model Hub handles routing. |
| 网关卡 | 模型网关 / 模型一次接入，网关统一路由 | Model Hub / Connect models once. Model Hub handles routing. |
| 检测到/已连接 key 行 | 检测到 API Key … / API Key … | API key detected … / API Key … |
| 添加更多 | 添加更多 / 订阅 / API Key | Add more / Subscription / API Key |
| 摘要 | 已添加 N 个提供商：… / 已选 N 个提供商：… · 仅复制，保留原配置 | N providers added: … / N providers selected: … |
| 发现胶囊 | 发现 N 个可导入模型网关的 API Key / 查看并导入 / 什么是模型网关？ / 关闭导入提示 | Found N API keys to import into Model Gateway / Review and import / What is Model Gateway? / Dismiss import notice |
| 导入弹窗 | 导入 API Key 到模型网关 / 导入选中的 N 项 / 正在导入 N 个 API Key… / 已导入 N 个 API Key / 还有 N 项未导入，可随时继续 / 继续选择 / 完成 | Import API keys to Model Gateway / Import N selected / Importing N API keys… / Imported N API keys / N remaining… / Review remaining / Done |
| 添加弹窗 | 添加订阅或 API Key / 添加更多模型提供商 / 已检测到 / 订阅 / 更多服务商 / 前往 ChatGPT/Claude 登录 / 添加 / 重试 / 取消 | Add subscription or API Key / Add more model providers / Detected / Subscription / More providers / Sign in with ChatGPT/Claude / Add / Retry / Cancel |
| 二屏主按钮 | 导入 N 项并继续 / 重试导入 N 项并继续 / 继续，选择 AI 助手 / 添加订阅或 API Key / 正在连接… / 正在检查连接… | Import N and continue / Retry importing N / Continue to assistants / Add subscription or API Key / Connecting… / Checking connection… |
| 返回 | 返回介绍 / 返回模型 | Back to introduction / Back to models |
| 三屏标题/副 | 选择你的 AI 助手 / 启用助手，确认默认模型，即可开始工作 | Choose your AI assistants / Enable an assistant, confirm its default model, and start working |
| 状态 pill | 已启用 / 未启用 / 未安装 / 安装中 / 升级中 / 已安装，未启用 | Enabled / Not enabled / Not installed / Installing / Updating / Installed, not enabled |
| 动作 | 立即安装 / 安装并启用 / 正在安装… / 立即升级 / 默认模型 | Install / Install and enable / Installing… / Update now / Default model |
| 路由弹窗 | 默认模型路由 / 首选 / 备用 N / 添加模型来源 / 完成 | Default model route / Preferred / Backup N / Add model source / Done |
| 三屏主按钮 | 进入工作台 | Open workspace |

## 9. 响应式规则

- ≥1600×950：主列 1200；h1 50、副 20；卡片 padding 28、logo 35；网关高 80；入口区 830 宽。
- max-height 1000 且 ≥761：紧凑档（主列 976、h1 34、卡高 262、CTA 高 44→动作区 52 等）。
- 761–1020：主列 `calc(100% - 48px)`，字号与 padding 收紧一档。
- ≤760（移动）：一屏卡片纵排、电路与回路字幕隐藏；二屏三列小卡（logo/名称/key 三行居中）、网关窄列、底部助手纵排三行、胶囊两行、CTA 全宽纵排；三屏卡片纵排可滚动；切屏回顶部。
- ≤360：胶囊与卡片再收紧（图标 16/24、内边距 10）。
- 矮屏桌面（max-height 800）：动作高 52、入口圆 48。
- 大屏垂直居中：内容超高时允许滚动，顶部留 `max(16px, (100dvh - 900px)/2)` 一类弹性留白。

## 10. 无障碍与键盘

- 切屏后焦点落到 h1（tabIndex -1）；摘要、状态 pill、胶囊文案、进度用 aria-live。
- 卡片/页签/提供商按钮用 aria-pressed；Switch 带 label；图标按钮带 aria-label；tooltip 用 aria-describedby + aria-expanded。
- 弹窗焦点陷阱，关闭后焦点回触发元素；Escape 与外部点击关闭菜单/tooltip/弹窗（进度中不可关导入弹窗）。
- 非当前屏内容用 hidden/inert 屏蔽焦点与读屏。
- 预览控制条（场景选择、暂停、重置、模拟失败）仅原型使用，不随产品 shipped。

## 11. 相邻已确认改动（同轮设计，合同条款）

- Settings 独立页：196px 设置导航栏 + 880px 内容列（32 水平 padding）+ 40px 返回行；无 Workbench 侧栏与 248px 偏移；直链/深链同样独立布局；返回恢复来源会话与未发送草稿；初始设置未完成回引导。
- Workbench 侧栏：品牌整体点击进新会话首页，副标题「Agent OS · Workbench」；删除独立新建会话行；搜索在品牌下全宽（保留 Cmd+K），其下收件箱全宽；项目标题保留添加项目；Agents/Skills 选中低饱和填充细描边、无外 glow；Apps 与设置置底；设置图标 1:1、Apps 相应加宽（09-20）。
- 首页：标题「今天想搭点什么？」+ 打开项目/管理 Agents/建后台任务；「<助手名> 已连接，可以开始工作」仅在首次真实连接成功后出现一次。
- Composer 两行：上行文本；下行附件、语音、Agent、工作区控件，Send 居右；图标按钮 28×28、选择器触发 28 高；hover/focus 覆盖整个目标且无位移；首页首条消息前附件/语音即可用；草稿与选择跨选择器取消和 Settings 往返保留。
- 浅色主题：实心绿按钮白字白图标；侧栏 Apps 控件为软绿填充 `#10B98129` + 绿描边/图标 + 常规文字（非实心主按钮）。

## 12. 设计稿画板索引（design_desktop.pen）

| 内容 | 画板 |
| --- | --- |
| 一屏 | bi8Au / P8iLm0 / J0qaZ；响应式 Hw8U2、wgbz7、FYMoC、GQvuC |
| 二屏 | i3vw9 / ktRgC / lm7mN；已添加 F8RLgR / xNmhX / r1IQ0B；响应式 p9BBt、kmZ1h、bD9bh、FYbSi |
| 三屏 | ICidc / HR3ps / S3mLYc；hover mK2JX / X7N2Z / ZaXoW；可升级 I9Q8VD / QPdWU / pHiuw；停用 GjGqO / tyLMM；响应式 V23IKo、KEvip、a1Ten、UuTup |
| 导入与帮助 | cGYku（发现/选择/进度/完成）、m2wv6X（帮助与关闭态） |
| 稳定框弹窗 | Cifke / e3UyJ / siyGC（错误与重试示例） |
| 响应式规则 | eq9QT |
| Settings / 侧栏 / 首页 | r6G6P / imxrT / IVZQS；DkNTK / m1yENk / ZAP8R / OCacc / k2FY0；n0PzOn / h8OBx / bobPL |

## 13. 已知差异与待确认（实现前 owner 一句话确认）

1. 底部三助手是否带「未启用」小字：设计稿有、预览没有。建议跟预览（只留 logo+名称），设计稿后续同步。
2. 网关英文文案：预览为 Model Hub / Connect models once. Model Hub handles routing.；设计稿英文板为 Model Gateway / Gateway handles routing。建议跟预览 i18n 字符串，设计稿英文板后续同步。
3. 发现胶囊：合同与设计稿都要求，当前预览未挂载（KeyImport 组件存在但未渲染）。建议实现时按合同恢复于第二屏（有未导入候选且未关闭时显示）。
4. 「添加更多」卡边框：预览虚线、设计稿实线。建议跟预览虚线，设计稿后续同步。
5. 旧按助手连接弹窗系列画板（V0zHJ…、JaGjt…、Cifke…）：三屏流程中 onboarding 不再按助手开连接弹窗；稳定框原则改用于第二屏三个弹窗。建议这些画板仅作为设置内连接管理参考，onboarding 不实现。

## 14. 验收清单

- 三屏切换：主按钮坐标/尺寸全程不变；返回保留全部状态；转场在 reduced-motion 下直接切换。
- 二屏：检测到候选默认选中且只有一个右侧对勾；切换选中不影响已添加底色；导入 N 项后卡变已添加样式、摘要与徽标计数正确；关闭胶囊后主按钮不跳动；tooltip 三种触发方式可用。
- 导入：空选择禁用；整批原子；失败重试保留选择；累计完成数；已导入不再出现；剩余可继续。
- 添加弹窗：页签切换外框不动；八家顺序与 logo；更多折叠；订阅仅 ChatGPT/Claude；空 key 禁用；错误保留输入。
- 三屏：安装/启用/升级/停用各状态独立呈现且只有一个加载指示；路由排序生效并共享；进入工作台 gating 正确；全未安装场景可走通。
- 响应式：1920×1080、1440×900、1366×768、390×844、375/320 窄屏、缩放与软键盘下无裁切、无横向溢出、动作可达。
- 主题与语言：dark ZH、dark EN、light ZH 对照设计稿；light EN 长文案不破坏布局。
- 相邻改动：Settings 直链独立布局且返回保留草稿；侧栏/首页/Composer 条款逐条对照。

## 15. 实现边界

- 独立 worktree、基于刷新后的 origin/master；保留无关本地改动。
- 复用现有 auth、Model Gateway scan/apply、媒体与路由基础设施；不做后端语义重构；缺展示字段时先定义最小兼容元数据。
- 所有新增用户文案走 `ui/src/i18n/en.json` / `zh.json`；不硬编码。
- 预览/演示数据只存在于原型；生产 UI 不显示模拟凭据说明。
- PR 走正常评审与 CI 门禁；合并需 owner 明确指令；验收在指定云环境。

# 给 AI Agent 的 Avibe 安装指南

这份文档可以直接丢给 Claude Code、Codex、OpenCode 或其他本地编码 agent，让它帮用户安装和配置 Avibe。

Avibe 会把本地 AI 编码 agent 接到 Slack、Discord、Telegram、微信、飞书 / Lark 等聊天平台。数据和 agent 进程都留在用户自己的机器上。

## 协助安装时的规则

- 选择聊天平台、agent 后端、工作区、凭证值之前，先问用户。
- 不要猜 token、API key、workspace ID、chat ID 等敏感配置。
- 优先使用运行 `vibe` 后打开的浏览器设置向导，不要手写配置文件。
- 如果本机已经有 `vibe` 服务在运行，除非用户确认安全，否则不要重启。
- 默认保持本地优先；除非平台本身要求，不要暴露公开 webhook。

## 第 1 步：检查机器环境

运行：

```bash
uname -a
command -v vibe || true
command -v uv || true
command -v claude || true
command -v opencode || true
command -v codex || true
```

Windows 用户默认推荐 WSL：

- https://github.com/avibe-bot/avibe/blob/master/docs/WINDOWS_WSL_ZH.md

## 第 2 步：安装 Avibe

macOS / Linux：

```bash
curl -fsSL https://avibe.bot/install.sh | bash
```

开源脚本，源码在 [GitHub](https://github.com/avibe-bot/avibe/blob/master/install.sh) 上；该短链只是到这个文件的 307 重定向。

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1 | iex
```

验证：

```bash
vibe version
vibe doctor
```

如果安装器无法使用 PyPI，会自动回退到 GitHub。如果已经安装但找不到 `vibe` 命令，查看安装器最后输出的 bin 目录，并帮用户把它加入 shell `PATH`。

## 第 3 步：至少安装一个编码 Agent

先问用户想用哪个后端。默认建议：OpenCode 上手最轻，Claude Code 适合复杂编码任务，Codex 适合 OpenAI 工作流。

OpenCode：

```bash
curl -fsSL https://opencode.ai/install | bash
```

如果用户接受权限取舍，可配置：

```json
{
  "permission": "allow"
}
```

Claude Code：

```bash
npm install -g @anthropic-ai/claude-code
```

Codex：

```bash
npm install -g @openai/codex
```

验证所选 agent：

```bash
opencode --version || true
claude --version || true
codex --version || true
```

## 第 4 步：启动设置向导

运行：

```bash
vibe
```

这会启动本地服务并打开 Web UI 设置向导。如果浏览器没有自动打开，查看终端输出里的本地 URL。

在向导里帮用户选择：

1. 聊天平台：Slack、Discord、Telegram、微信、飞书 / Lark。
2. Agent 后端：Claude Code、OpenCode、Codex。
3. 项目工作目录。
4. 需要启用的频道或聊天范围。

平台文档：

- Slack: https://github.com/avibe-bot/avibe/blob/master/docs/SLACK_SETUP_ZH.md
- Discord: https://github.com/avibe-bot/avibe/blob/master/docs/DISCORD_SETUP_ZH.md
- Telegram: https://github.com/avibe-bot/avibe/blob/master/docs/TELEGRAM_SETUP_ZH.md
- 微信：使用应用内向导。
- 飞书 / Lark：使用应用内向导。

## 第 5 步：可选的远程 Web UI

如果用户想从手机、平板或远端机器打开本机 Web UI，运行：

```bash
vibe remote
```

它会引导用户登录 avibe.bot、完成配对并启动安全 tunnel。这个能力用于访问 Web UI，不是把 agent runtime 直接暴露到公网。

## 第 6 步：冒烟测试

配置完成后，让用户在启用的聊天里发一条短消息：

```text
Say hello and tell me which project directory you are running in.
```

然后验证：

```bash
vibe status
```

如果消息没有到达，运行：

```bash
vibe doctor
```

再按平台文档检查权限、bot 隐私设置、频道选择等问题。

## 常见问题

| 现象 | 检查点 |
| --- | --- |
| 找不到 `vibe` 命令 | shell `PATH`、uv tool bin 目录、安装器输出 |
| 找不到 agent | 安装所选 agent CLI，并确认它在 `PATH` 里 |
| Slack 没反应 | App 是否安装、Socket Mode token、bot token scopes、bot 是否被邀请进频道 |
| Telegram 群里没反应 | Bot privacy mode、是否需要 @、聊天是否已被发现 |
| Discord 没反应 | Bot token、服务器/频道选择、gateway intents |
| 手机打不开 Web UI | 使用 `vibe remote`；localhost 只适合同一台机器 |

## 卸载

只有用户明确要删除 Avibe 时才运行：

```bash
vibe stop
uv tool uninstall vibe-remote
rm -f ~/.local/bin/vibe
rm -rf "${AVIBE_HOME:-$HOME/.avibe}/runtime/install-generations"
rm -rf ~/.vibe_remote
```

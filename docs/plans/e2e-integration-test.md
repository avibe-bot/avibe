# E2E Integration Test Plan

> **Status**: Planning
> **Goal**: 真正的端到端测试 — Docker 部署 + Web UI Playwright + 三平台 API 驱动集成测试

## 1. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Docker Container (System Under Test)                        │
│                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────────────────┐│
│  │ UI Server│  │ vibe-remote  │  │ Agent Backends           ││
│  │ :5123    │  │ Bot A (SUT)  │  │ opencode / claude / codex││
│  └─────┬────┘  └──────┬───────┘  └─────────────────────────┘│
│        │               │                                     │
└────────┼───────────────┼─────────────────────────────────────┘
         │               │
    Playwright      Platform APIs (Slack/Discord/Feishu)
         │               │
┌────────┴───────────────┴─────────────────────────────────────┐
│  Test Runner (Host / CI)                                     │
│                                                              │
│  pytest tests/e2e/                                           │
│  ├── test_health.py         # 已有: API smoke tests          │
│  ├── test_api.py            # 已有: config/status/doctor     │
│  ├── test_settings.py       # 已有: settings/logs            │
│  ├── test_users_and_bind.py # 已有: users/bind codes         │
│  ├── test_ui_playwright.py  # 新增: Web UI flows             │
│  ├── test_slack.py          # 新增: Slack API 驱动           │
│  ├── test_discord.py        # 新增: Discord API 驱动         │
│  └── test_feishu.py         # 新增: Feishu API 驱动          │
│                                                              │
│  tests/e2e/drivers/                                          │
│  ├── base.py                # PlatformDriver ABC             │
│  ├── slack_driver.py        # slack_sdk Bot B                │
│  ├── discord_driver.py      # discord HTTP API Bot B         │
│  └── feishu_driver.py       # lark_oapi Bot B                │
└──────────────────────────────────────────────────────────────┘
```

## 2. Two-Bot Model

Each platform has two bots in the same test channel:

| Role | 描述 | 行为 |
|------|------|------|
| **Bot A (SUT)** | vibe-remote bot, 运行在 Docker 中 | 监听消息 → 调 agent → 回复 |
| **Bot B (Test Driver)** | 独立的 bot, 由 pytest 控制 | 发消息 → 等待 Bot A 回复 → 断言 |

### Bot B 需要的权限

**Slack Bot B:**
- Scopes: `chat:write`, `channels:history`, `channels:read`, `groups:history`
- 需要: Bot Token (`xoxb-...`)
- 不需要 App Token (不用 Socket Mode, 只用 Web API 读写)

**Discord Bot B:**
- Permissions: Send Messages, Read Message History, Read Messages
- Intent: Message Content Intent (privileged, 需要在 Developer Portal 开启)
- 需要: Bot Token

**飞书 Bot B:**
- 权限: `im:message:send_as_bot`, `im:message:receive_as_bot`, `im:chat:readonly`
- 需要: App ID + App Secret
- 用 HTTP API 发消息和拉取消息历史

## 3. Test Driver Framework

### 3.1 Base Driver Interface

```python
class PlatformDriver(ABC):
    """Platform-agnostic test driver interface."""

    @abstractmethod
    async def send_message(self, channel_id: str, text: str) -> str:
        """Send a message as Bot B. Returns message ID."""

    @abstractmethod
    async def wait_for_reply(
        self,
        channel_id: str,
        after_message_id: str,
        bot_a_id: str,
        timeout: float = 60,
        poll_interval: float = 2,
    ) -> str:
        """Poll channel history for Bot A's reply after a given message.
        Returns reply text. Raises TimeoutError if no reply within timeout."""

    @abstractmethod
    async def send_command(self, channel_id: str, command: str, args: str = "") -> str:
        """Send a slash-style command (e.g., /settings). Returns message ID."""

    @abstractmethod
    async def get_thread_replies(self, channel_id: str, thread_id: str) -> list[dict]:
        """Get all replies in a thread."""
```

### 3.2 Wait-for-Reply Strategy

核心挑战: Bot A 的回复是异步的, 测试需要轮询.

```python
async def wait_for_reply(self, channel_id, after_ts, bot_a_id, timeout=60, poll_interval=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = await self._get_messages_after(channel_id, after_ts)
        for msg in messages:
            if msg["user"] == bot_a_id and msg["ts"] > after_ts:
                return msg["text"]
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"Bot A did not reply within {timeout}s")
```

因为用 Real Agent, timeout 设为 120s (LLM 响应 + 网络延迟).

## 4. Docker Infrastructure Changes

### 4.1 Dockerfile: Add UI build + Agent CLIs

```dockerfile
# Stage 1: Build UI
FROM node:20-slim AS ui-builder
WORKDIR /app/ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ .
RUN npm run build

# Stage 2: Python app + agents
FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
COPY --from=ui-builder /app/ui/dist /app/ui/dist

# Install vibe-remote
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0
RUN SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION} \
    pip install --no-cache-dir -e .

# Install agent CLIs
# OpenCode: npm install
RUN npm install -g @anthropic-ai/opencode || true
# Claude Code: npm install
RUN npm install -g @anthropic-ai/claude-code || true
# Codex: pip install
RUN pip install --no-cache-dir codex-cli || true
```

### 4.2 docker-compose.e2e.yml: Full mode + env injection

```yaml
services:
  vibe:
    build: .
    command: ["full"]
    ports:
      - "${VIBE_E2E_PORT:-15123}:5123"
    environment:
      - AVIBE_HOME=/data/avibe
      - VIBE_UI_PORT=5123
      # Platform tokens (Bot A - SUT)
      - SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-}
      - SLACK_APP_TOKEN=${SLACK_APP_TOKEN:-}
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
      - LARK_APP_ID=${LARK_APP_ID:-}
      - LARK_APP_SECRET=${LARK_APP_SECRET:-}
      # Agent API keys
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
    volumes:
      - vibe-e2e-data:/data/avibe
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5123/health', timeout=3)"]
      interval: 3s
      timeout: 5s
      retries: 20
      start_period: 30s

volumes:
  vibe-e2e-data:
```

### 4.3 .env.e2e.example (template for secrets)

```env
# === Bot A (SUT - vibe-remote bot) ===
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
DISCORD_BOT_TOKEN=...
LARK_APP_ID=cli_...
LARK_APP_SECRET=...

# === Bot B (Test Driver) ===
E2E_SLACK_BOT_B_TOKEN=xoxb-...
E2E_DISCORD_BOT_B_TOKEN=...
E2E_FEISHU_BOT_B_APP_ID=cli_...
E2E_FEISHU_BOT_B_APP_SECRET=...

# === Test Channels ===
E2E_SLACK_CHANNEL=C...
E2E_DISCORD_CHANNEL=...
E2E_FEISHU_CHAT_ID=oc_...

# === Bot A User IDs (for reply detection) ===
E2E_SLACK_BOT_A_ID=U...
E2E_DISCORD_BOT_A_ID=...
E2E_FEISHU_BOT_A_ID=ou_...

# === Agent API Keys ===
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

## 5. Simulate-Interaction Endpoint (方案 B)

### 5.1 Problem

Bot B 能发消息和文本命令, 但无法点击 Bot A 发送的按钮/菜单.
三个平台都没有 "代替用户点击按钮" 的 API.

### 5.2 Solution

在容器内暴露一个测试专用端点, 仅在 `E2E_TEST_MODE=true` 时注册.
该端点直接调用 controller 的 handler 链, 跳过平台的 interaction 传递机制.

```python
# vibe/ui_server.py (仅 E2E_TEST_MODE=true 时注册)
@app.route("/e2e/simulate-interaction", methods=["POST"])
def simulate_interaction():
    """Simulate a platform button click / modal submit.

    Directly invokes the controller handler chain, bypassing
    the platform's interaction delivery mechanism.
    """
    payload = request.json
    # Required fields:
    #   action: str          — callback_data (e.g. "cmd_settings", "cmd_routing")
    #   user_id: str         — who "clicked"
    #   channel_id: str      — where
    #   is_dm: bool          — DM context
    # Optional fields:
    #   modal_values: dict   — for modal submissions (settings save, routing save)
    #   thread_id: str       — thread context
```

### 5.3 Supported Simulation Types

| Type | action 值 | 模拟的行为 |
|------|----------|-----------|
| Button click | `cmd_settings`, `cmd_routing`, `cmd_change_cwd`, `cmd_resume`, `cmd_clear` | 点击 /start 菜单中的按钮 |
| Settings save | `settings_submit` + `modal_values` | Settings modal 提交 |
| Routing save | `routing_submit` + `modal_values` | Routing modal 提交 |
| CWD submit | `cwd_submit` + `modal_values.cwd` | Change CWD modal 提交 |

### 5.4 Security

- 端点仅在 `E2E_TEST_MODE=true` 环境变量下注册
- 生产环境不存在该路由, 无安全风险
- Docker compose 中显式设置: `E2E_TEST_MODE=true`

### 5.5 Test Flow Example

```
1. Bot B 发 "/start" → Bot A 回复带按钮菜单
2. Test: POST /e2e/simulate-interaction
         {"action": "cmd_settings", "user_id": "U...", "channel_id": "C..."}
3. Bot A 处理 settings 回调 → 在 channel 中发送 settings 消息/modal
4. Bot B 读取 channel 历史 → 断言 settings 内容正确
5. Test: POST /e2e/simulate-interaction
         {"action": "settings_submit", "user_id": "U...", "channel_id": "C...",
          "modal_values": {"show_message_types": ["system", "assistant"]}}
6. Bot A 保存 settings → 发送确认消息
7. Bot B 读取确认消息 → 断言保存成功
```

## 6. Test Cases

### 6.1 Platform Integration Tests (per platform × same cases)

| # | Test Case | 操作 | 预期 |
|---|-----------|------|------|
| 1 | **Basic message → reply** | Bot B 发 "hello, respond with PONG" | Bot A 回复包含 "PONG" |
| 2 | **Thread continuation** | Bot B 在同一 thread 继续发消息 | Bot A 在同一 thread 回复 |
| 3 | **Command: /start** | Bot B 发 "/start" | Bot A 发送欢迎消息 |
| 4 | **Command: /cwd** | Bot B 发 "/cwd" | Bot A 回复当前 CWD |
| 5 | **Command: /clear** | Bot B 发 "/clear" | Bot A 确认清除 |
| 6 | **DM bind flow** | Bot B 在 DM 中发 "/bind <code>" | Bot A 确认绑定成功 |
| 7 | **Unbound DM rejected** | 未绑定用户在 DM 发消息 | Bot A 拒绝并提示 bind |

### 6.2 Button/Modal Simulation Tests (via /e2e/simulate-interaction)

| # | Test Case | 操作 | 预期 |
|---|-----------|------|------|
| 1 | **Settings button** | simulate cmd_settings → 读 Bot A 回复 | Bot A 发送 settings 信息 |
| 2 | **Settings save** | simulate settings_submit + modal_values | Bot A 确认 "settings updated" |
| 3 | **Routing button** | simulate cmd_routing | Bot A 发送 routing 信息 |
| 4 | **Routing save** | simulate routing_submit + backend=opencode | Bot A 确认 "routing updated" |
| 5 | **Change CWD** | simulate cwd_submit + cwd=/tmp/test | Bot A 确认 CWD 变更 |
| 6 | **Admin guard** | simulate cmd_settings as non-admin | Bot A 拒绝 "not admin" |

### 6.3 Web UI Playwright Tests

| # | Test Case | 操作 | 预期 |
|---|-----------|------|------|
| 1 | **Setup wizard** | 打开首页 → 完成配置 | 配置保存成功 |
| 2 | **Users page** | 进入 Users → 生成绑定码 | 绑定码出现在列表 |
| 3 | **Settings page** | 修改 settings → 保存 | API 确认保存 |

### 6.4 Cross-concern Tests

| # | Test Case | 操作 | 预期 |
|---|-----------|------|------|
| 1 | **UI 生成绑定码 → 平台 bind** | UI 创建码 → Bot B 在 DM 发 /bind <code> | 绑定成功, UI 显示已绑定 |
| 2 | **UI 改 routing → 平台验证** | UI 切换到 codex → Bot B 发消息 | 响应来自 codex agent |

## 7. Configuration Injection

容器启动时需要自动注入配置. entrypoint 改造:

```bash
# docker-entrypoint.sh 新增 (full mode 下)
# Auto-configure from env vars if config.json doesn't have platform set up
if [ -n "$SLACK_BOT_TOKEN" ]; then
    python -c "
from vibe.runtime import ...
# Write Slack config with tokens from env
"
fi
```

或者: 测试 runner 在容器启动后通过 `POST /config` API 注入配置.
**推荐后者** — 更灵活, 不需要改 entrypoint.

## 8. Execution Flow

```
1. pytest 收集 test_slack.py / test_discord.py / test_feishu.py
2. conftest.py session fixture:
   a. docker compose up (full mode, 带平台 tokens)
   b. wait for /health OK
   c. POST /config 注入平台配置
   d. wait for bot to connect (poll /status until running=true)
3. 每个 test case:
   a. driver.send_message(channel, prompt)
   b. reply = driver.wait_for_reply(channel, msg_id, bot_a_id, timeout=120)
   c. assert "expected" in reply
4. teardown: docker compose down
```

## 9. CI Integration (Future)

GitHub Actions workflow:
- Secrets: 所有 tokens 存为 GitHub Secrets
- Trigger: `workflow_dispatch` (手动) + nightly schedule
- 不放在 PR CI 里 (太慢, 太贵)

## 10. Prerequisites (User Action Required)

### 需要你创建的:

1. **Slack 测试 workspace** (免费版即可)
   - 安装 Bot A (vibe-remote)
   - 创建 Bot B Slack App, 安装到同一 workspace
   - 创建 `#e2e-test` channel, 邀请两个 bot

2. **Discord Bot B**
   - 在 Discord Developer Portal 创建新 Application
   - 开启 Message Content Intent
   - 生成 Bot Token
   - 邀请到测试 server, 和 Bot A 在同一个 channel

3. **飞书 Bot B**
   - 在飞书开放平台创建新应用
   - 配置消息相关权限
   - 安装到测试企业
   - 在测试群里加入 Bot A 和 Bot B

4. **收集 tokens** 填入 `.env.e2e` (参考 Section 4.3 的模板)

## 11. Implementation Order

| Phase | 内容 | 依赖 |
|-------|------|------|
| **P0** | Dockerfile 改造 (UI build + agent CLIs) | 无 |
| **P1** | Test driver framework (`drivers/base.py`) | 无 |
| **P2** | Slack driver + test cases | P0 + P1 + Slack tokens |
| **P3** | Discord driver + test cases | P0 + P1 + Discord tokens |
| **P4** | Feishu driver + test cases | P0 + P1 + Feishu tokens |
| **P5** | Playwright UI tests | P0 |
| **P6** | Cross-concern tests | P2-P5 |

P0 和 P1 我现在就可以做. P2-P4 需要你准备好 tokens 才能实际调试.

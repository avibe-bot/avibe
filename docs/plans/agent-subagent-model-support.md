# Claude Code / Codex 模型和配置支持方案

> **最后更新**: 2026-01-27
> **状态**: 待实施

## 背景

当前 vibe-remote 项目已经为 OpenCode 实现了完整的配置支持（subagent、model、reasoning effort）。
但 Claude Code 和 Codex 在 UI 中选择后，没有对应的配置选项。

> Historical note: this plan predates Agent-first routing. The old scope
> backend route field is deprecated and ignored; UI should derive backend from
> the selected Vibe Agent.

## 目标

为 Claude Code 和 Codex 添加与 OpenCode 相同的 UI 配置入口，支持 **Web UI** 和 **Slack Settings** 两种方式。

## 功能支持范围

| Agent | Subagent | Model | Reasoning Effort |
|-------|:--------:|:-----:|:----------------:|
| **OpenCode** | ✅ | ✅ | ✅ |
| **Claude Code** | ✅ | ✅ | ❌ (无 CLI 参数) |
| **Codex** | ✅ | ✅ | ✅ |

---

## 一、数据结构变更

### 1.1 RoutingSettings 扩展

**文件：`config/v2_settings.py`**

```python
@dataclass
class RoutingSettings:
    # 通用
    agent_name: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    
    # OpenCode 特定 (现有)
    opencode_agent: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_reasoning_effort: Optional[str] = None
    
    # Claude Code 特定 (新增)
    claude_agent: Optional[str] = None
    claude_model: Optional[str] = None
    # 注：Claude 没有 reasoning_effort CLI 参数
    
    # Codex 特定 (新增)
    codex_agent: Optional[str] = None
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None
```

### 1.2 前端 ChannelConfig 类型扩展

**文件：`ui/src/components/steps/ChannelList.tsx`**

```typescript
interface ChannelConfig {
  enabled: boolean;
  show_message_types: string[];
  custom_cwd: string;
  routing: {
    agent_name: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    // OpenCode (现有)
    opencode_agent?: string | null;
    opencode_model?: string | null;
    opencode_reasoning_effort?: string | null;
    // Claude Code (新增)
    claude_agent?: string | null;
    claude_model?: string | null;
    // Codex (新增)
    codex_agent?: string | null;
    codex_model?: string | null;
    codex_reasoning_effort?: string | null;
  };
  require_mention?: boolean | null;
}
```

---

## 二、UI 变更

### 2.1 Claude Code 配置面板

在 `ChannelList.tsx` 中，OpenCode Settings 后面添加：

```tsx
{/* Claude Code Settings */}
{selectedAgent?.backend === 'claude' && (
  <div className="space-y-3">
    <div className="text-xs font-medium text-muted uppercase">{t('channelList.claudeSettings')}</div>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 bg-bg/50 p-3 rounded border border-border">
      {/* Agent 选择 */}
      <div className="space-y-1">
        <label className="text-xs text-muted">{t('channelList.agent')}</label>
        <select
          value={channelConfig.routing.claude_agent || ''}
          onChange={(e) =>
            updateConfig(channel.id, {
              routing: { ...channelConfig.routing, claude_agent: e.target.value || null },
            })
          }
          className="w-full bg-panel border border-border rounded px-3 py-2 text-sm"
        >
          <option value="">{t('common.default')}</option>
          {(claudeAgents || []).map((agent: string) => (
            <option key={agent} value={agent}>{agent}</option>
          ))}
        </select>
      </div>
      {/* Model 选择 */}
      <div className="space-y-1">
        <label className="text-xs text-muted">{t('channelList.model')}</label>
        <select
          value={channelConfig.routing.claude_model || ''}
          onChange={(e) =>
            updateConfig(channel.id, {
              routing: { ...channelConfig.routing, claude_model: e.target.value || null },
            })
          }
          className="w-full bg-panel border border-border rounded px-3 py-2 text-sm"
        >
          <option value="">{t('common.default')}</option>
          <option value="claude-sonnet-4">Claude Sonnet 4</option>
          <option value="claude-opus-4">Claude Opus 4</option>
          <option value="claude-3-5-haiku-20241022">Claude Haiku</option>
        </select>
      </div>
      {/* 注意：不显示 Reasoning Effort - Claude Code 无 CLI 参数支持 */}
    </div>
  </div>
)}
```

### 2.2 Codex 配置面板

```tsx
{/* Codex Settings */}
{selectedAgent?.backend === 'codex' && (
  <div className="space-y-3">
    <div className="text-xs font-medium text-muted uppercase">{t('channelList.codexSettings')}</div>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 bg-bg/50 p-3 rounded border border-border">
      {/* Model 选择 */}
      <div className="space-y-1">
        <label className="text-xs text-muted">{t('channelList.model')}</label>
        <select
          value={channelConfig.routing.codex_model || ''}
          onChange={(e) =>
            updateConfig(channel.id, {
              routing: { ...channelConfig.routing, codex_model: e.target.value || null },
            })
          }
          className="w-full bg-panel border border-border rounded px-3 py-2 text-sm"
        >
          <option value="">{t('common.default')}</option>
          <option value="gpt-5-codex">GPT-5 Codex</option>
          <option value="o3">o3</option>
          <option value="o4-mini">o4-mini</option>
          <option value="gpt-4o">GPT-4o</option>
        </select>
      </div>
      {/* Reasoning Effort 选择 */}
      <div className="space-y-1">
        <label className="text-xs text-muted">{t('channelList.reasoningEffort')}</label>
        <select
          value={channelConfig.routing.codex_reasoning_effort || ''}
          onChange={(e) =>
            updateConfig(channel.id, {
              routing: { ...channelConfig.routing, codex_reasoning_effort: e.target.value || null },
            })
          }
          className="w-full bg-panel border border-border rounded px-3 py-2 text-sm"
        >
          <option value="">{t('common.default')}</option>
          <option value="minimal">Minimal</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="xhigh">Extra High</option>
        </select>
      </div>
    </div>
  </div>
)}
```

### 2.3 UI 配置项可见性总结

| 配置项 | OpenCode | Claude Code | Codex |
|--------|:--------:|:-----------:|:-----:|
| Agent/Subagent | ✅ 显示 | ✅ 显示 | ✅ 显示 |
| Model | ✅ 显示 | ✅ 显示 | ✅ 显示 |
| Reasoning Effort | ✅ 显示 | ❌ 不显示 | ✅ 显示 |

---

## 三、后端变更

### 3.1 Claude Code Agent 修改

**文件：`core/handlers/session_handler.py`**

```python
async def get_or_create_claude_session(
    self,
    context: MessageContext,
    subagent_name: Optional[str] = None,
    subagent_model: Optional[str] = None,
) -> ClaudeSDKClient:
    # 读取 Channel 级别覆盖
    channel_settings = self.settings_manager.get_channel_settings(context.channel_id)
    routing = channel_settings.routing if channel_settings else None
    
    # 优先级：参数 > channel 配置 > 全局默认
    agent = subagent_name or (routing.claude_agent if routing else None)
    model = subagent_model or (routing.claude_model if routing else None) or self.config.claude.default_model
    
    extra_args = {}
    if agent:
        extra_args["agent"] = agent
    if model:
        extra_args["model"] = model
    
    # 创建/获取 session...
```

### 3.2 Codex Agent 修改

**文件：`modules/agents/codex_agent.py`**

```python
def _build_command(
    self,
    prompt: str,
    working_path: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> List[str]:
    cmd = [
        self.codex_config.cli_path,
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
    ]

    # 模型选择
    if model:
        cmd.extend(["--model", model])

    # 推理强度 (官方支持的 CLI 参数)
    if reasoning_effort:
        cmd.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])

    cmd.extend(["--cd", working_path])
    cmd.append(prompt)
    return cmd


async def handle_message(self, request: AgentRequest) -> None:
    # 读取 Channel 级别覆盖
    channel_settings = self.settings_manager.get_channel_settings(request.context.channel_id)
    routing = channel_settings.routing if channel_settings else None
    
    model = (routing.codex_model if routing else None) or self.codex_config.default_model
    reasoning_effort = (routing.codex_reasoning_effort if routing else None) or self.codex_config.default_reasoning_effort
    
    cmd = self._build_command(
        prompt=request.message,
        working_path=request.working_path,
        session_id=stored_session_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    # ...
```

### 3.3 API 端点：获取 Claude Agents

需要新增 API 端点，扫描用户的 Claude agents 目录：

**文件：`vibe/api.py`** (或相应位置)

```python
@app.get("/api/claude/agents")
async def get_claude_agents():
    """扫描 ~/.claude/agents/ 目录，返回可用的 agent 列表"""
    agents = []
    agents_dir = Path.home() / ".claude" / "agents"
    if agents_dir.exists():
        for md_file in agents_dir.glob("*.md"):
            agents.append(md_file.stem)
    return {"ok": True, "agents": agents}
```

---

## 四、i18n 翻译

**文件：`ui/src/i18n/en.json`**

```json
{
  "channelList": {
    "claudeSettings": "Claude Code Settings",
    "codexSettings": "Codex Settings"
  }
}
```

**文件：`ui/src/i18n/zh.json`**

```json
{
  "channelList": {
    "claudeSettings": "Claude Code 设置",
    "codexSettings": "Codex 设置"
  }
}
```

---

## 五、实施步骤

### Phase 1: 数据结构 (0.5 天)

- [ ] 1.1 修改 `config/v2_settings.py`：添加 Claude/Codex 路由字段
- [ ] 1.2 修改 `modules/settings_manager.py`：支持新字段的读写

### Phase 2: 后端 Agent 支持 (1 天)

- [ ] 2.1 修改 `core/handlers/session_handler.py`：Claude 读取 channel 配置
- [ ] 2.2 修改 `modules/agents/codex_agent.py`：添加 model 和 reasoning_effort 支持
- [ ] 2.3 新增 API `/api/claude/agents`：扫描 Claude agents 目录

### Phase 3: 前端 UI (1 天)

- [ ] 3.1 修改 `ChannelList.tsx`：添加 Claude Settings 面板
- [ ] 3.2 修改 `ChannelList.tsx`：添加 Codex Settings 面板
- [ ] 3.3 修改 `ChannelList.tsx`：加载 Claude agents 列表
- [ ] 3.4 修改 `Summary.tsx`：保存新的配置字段

### Phase 4: i18n 和测试 (0.5 天)

- [ ] 4.1 添加 i18n 翻译 (en.json, zh.json)
- [ ] 4.2 端到端测试各 backend 配置
- [ ] 4.3 构建 UI (`npm run build`)

**预计总工时**: 3 天

---

## 六、Channel Settings JSON 示例

```json
{
  "channels": {
    "C12345": {
      "enabled": true,
      "routing": {
        "agent_name": "claude",
        "claude_agent": "reviewer",
        "claude_model": "claude-sonnet-4"
      }
    },
    "C67890": {
      "enabled": true,
      "routing": {
        "agent_name": "codex",
        "codex_model": "o3",
        "codex_reasoning_effort": "high"
      }
    },
    "C11111": {
      "enabled": true,
      "routing": {
        "agent_name": "opencode",
        "opencode_agent": "build",
        "opencode_model": "anthropic/claude-sonnet-4-20250514",
        "opencode_reasoning_effort": "medium"
      }
    }
  }
}
```

---

## 七、CLI 参数参考

### Claude Code

```bash
claude --model claude-sonnet-4 --agent reviewer "your prompt"
```

- `--model`: 模型选择
- `--agent`: subagent 选择 (需要在 ~/.claude/agents/ 中定义)

### Codex

```bash
codex exec --model o3 -c model_reasoning_effort=high "your prompt"
```

- `--model` / `-m`: 模型选择
- `-c model_reasoning_effort=<value>`: 推理强度 (minimal/low/medium/high/xhigh)

---

## 八、注意事项

1. **Claude Code 没有 Reasoning Effort CLI 参数**
   - Extended Thinking 只能通过 `~/.claude/settings.json` 配置
   - UI 中不显示该选项

2. **Codex 现已支持 Subagent**
   - 通过 `routing.codex_agent` 和前缀路由选择
   - 支持从 Codex agent 定义继承默认 model / reasoning

3. **向后兼容**
   - 所有新字段使用 Optional 类型
   - 现有 OpenCode 配置不受影响

# 语音附件 ASR 计划

## 背景

现在各 IM 平台收到文件后，会先把附件归一化成 `FileAttachment`，
再由共享的 `MessageHandler._process_file_attachments` 下载到本地，
最后放进 `AgentRequest.files`。最终给 Agent 看的 `[User Attachments]`
区块目前是在各个 agent backend 内部渲染的：

- Claude / OpenCode 在 `_prepare_message_with_files` 里追加附件文本。
- Codex 会构造文本输入，同时把图片作为原生 `localImage` 输入传入。

所以语音转文字不应该写在某一个 IM 平台适配器里，也不应该写在
Claude、Codex 或 OpenCode 某一个 backend 里。正确位置是在附件下载完成之后、
`AgentRequest` 创建之前。这样所有平台和所有 agent backend 都能继承同一套行为。

## 目标

当用户发送语音文件附件时：

1. 继续按现有逻辑下载并保存附件。
2. 对符合条件的语音文件，调用 AVIBE 后端的 OpenAI Compatible ASR 接口。
3. 在正常 turn pipeline 里最多等待 60 秒。
4. 如果 ASR 在 60 秒内成功完成，把转写文本追加到用户消息里，同时继续把原始附件放进 `AgentRequest.files`。
5. 如果 ASR 超时或失败，保持现有行为：用户文本加原本的附件说明，不额外暴露错误。
6. 在 Web UI 的 Messaging 设置页提供一个开关。
7. 在 ASR 开关旁边加一个“是否回显转写结果”的开关；默认开启回显。
8. 借这个功能顺手重组 Messaging 设置页，不再把新功能继续塞进当前越来越长的大面板里。

## 非目标

- 不在 Vibe Remote 本地实现语音识别。
- 不让本地客户端直接调用 DashScope / Qwen。
- 不改变 Agent 读取已下载附件路径的方式。
- 除非平台无法提供可用附件，否则不做平台专属语音逻辑。
- ASR 失败默认不展示给用户或 Agent；失败只是 fallback 条件。
- 不在 Vibe Remote 里新增用户自管的 Qwen / DashScope API Key。local runtime 只调用 AVIBE，AVIBE 负责上游模型凭据。

## 当前流程

```text
IM adapter
  -> MessageContext(files=[FileAttachment(...)] )
  -> MessageHandler._handle_turn()
  -> _process_file_attachments()
      保存到 ~/.vibe_remote/attachments/<channel_id>/<timestamp>_<name>
  -> _prepend_message_metadata()
  -> _append_attachment_errors()
  -> AgentRequest(message=..., files=processed_files)
  -> agent backend 渲染 [User Attachments]
```

ASR 步骤应该插在这里：

```text
_process_file_attachments()
  -> _augment_message_with_audio_transcripts()
  -> _prepend_message_metadata()
  -> _append_attachment_errors()
```

这样 ASR 保持平台无关、backend 无关。

## 方案设计

### 1. 新增共享 ASR 服务

新增一个小的 core 服务，例如 `core/audio_asr.py`，包含三部分：

- 判断附件是否是可转写音频
- 调用 AVIBE OpenAI Compatible 转写接口
- 把转写结果格式化并追加到消息里

这个服务只接收下载后的 `FileAttachment`，返回转写结果列表。它不应该知道 Slack、Telegram、Claude、Codex 或 OpenCode。

建议结构：

```python
@dataclass
class AudioTranscript:
    attachment_name: str
    local_path: str
    text: str
    duration_ms: int | None = None


class AudioAsrService:
    async def transcribe_attachments(
        self,
        attachments: list[FileAttachment],
        *,
        timeout_seconds: float = 60.0,
    ) -> list[AudioTranscript]:
        ...
```

`MessageHandler` 负责每个 turn 的 60 秒 deadline，并捕获所有 ASR 异常。
超时或失败时只记录简洁 warning，然后继续走没有转写结果的老路径。

### 2. 音频判断规则

满足以下任一条件就认为是音频附件：

- `mimetype` 以 `audio/` 开头
- 文件扩展名属于 AVIBE 后端 / Qwen ASR 支持的格式：`aac`、`flac`、`m4a`、`mp3`、`mp4`、`ogg`、`opus`、`wav`、`webm`

重要边界：微信有时会直接在 `voice_item.text` 里给出平台自带转写文本，
Vibe Remote 现在已经复用了这段官方转写。第一版不要支持微信 `audio/silk`。
如果微信语音没有平台自带文本，就继续走当前“只有附件说明”的 fallback，
不要为了它新增本地或后端 Silk 转码。

### 3. AVIBE 后端 API 契约

使用 OpenAI Compatible 的 audio transcription 契约。建议本地客户端请求：

```http
POST {backend_url}/v1/audio/transcriptions
Content-Type: multipart/form-data

model=qwen3-asr-flash
file=@voice.m4a
response_format=json
```

期望响应：

```json
{
  "text": "transcribed speech..."
}
```

产品 route 不要带 `openai`。接口可以是 OpenAI-compatible，但路径应该是 AVIBE 自己的路径。

鉴权应该复用 pairing 后已经存在的 remote-access device credential：

- pairing 会返回长期有效的 `instance_secret`
- Vibe Remote 把它保存在 `remote_access.vibe_cloud.instance_secret`
- AVIBE 后端只保存它的 hash，也就是 `remoteAccessDevices.deviceSecretHash`
- 现有 `runtime-status` route 已经通过 `getInstanceByDeviceSecret()` 验证它，同时要求 device 未撤销、instance 处于 active 状态

推荐 ASR 鉴权方式：

```http
X-Vibe-Instance-Id: <instance_id>
X-Vibe-Device-Secret: <instance_secret>
```

后端收到后 hash `X-Vibe-Device-Secret`，复用同一个 `getInstanceByDeviceSecret()` store 方法校验，成功后更新 `lastSeenAt`，并且要求最近一次 runtime status 显示 `tunnel_running=true`。如果没有最近 runtime status，或者 tunnel 不在运行中，ASR 返回明确的 403 或 409。这样 ASR 会绑定到“已经配对且本地 Vibe Remote 正在运行”的设备。

这是更适合长期使用的方案：

- 复用 pairing 时已经发放的一份凭据
- 只要 device 没被撤销、instance 没被停用、用户没重新 pairing，就长期有效
- 不依赖浏览器 OAuth session 或会过期的 ID / access token
- ASR 只开放给已配对 Vibe Cloud 的本地 runtime，不引入用户自管 API key
- 不滥用 Cloudflare tunnel token；tunnel token 负责连接，device secret 才是 local runtime 调后端 API 的凭据

建议后端新增一个小 helper，例如 `requireDeviceRuntime()`，让 `runtime-status` 和 ASR 共用同一套 device credential 校验。`runtime-status` 可以为了兼容继续接受 JSON body 里的 `instance_secret`，但 ASR 是 multipart 请求，鉴权更适合放在 header 里。

### 4. 配置

新增本地配置块，不把常量埋在 `MessageHandler` 里：

```python
@dataclass
class AudioAsrConfig:
    enabled: bool = False
    echo_transcript: bool = True
    timeout_seconds: float = 60.0
    endpoint_path: str = "/v1/audio/transcriptions"
    model: str = "qwen3-asr-flash"
    max_file_bytes: int | None = None
```

默认 endpoint base 来自 `config.remote_access.vibe_cloud.backend_url`，
默认是 `https://avibe.bot`。如果还没有配对 Vibe Cloud，或者缺少 `instance_secret`，
就跳过 ASR，保持当前行为。

默认值建议先设为 **关闭**，等 AVIBE endpoint 上线并验证后再决定是否默认打开。
开关属于 Messaging，因为它改变的是“用户消息进入 Agent 前怎么准备”。
它不应该放在某个具体 backend 下，因为 Claude、Codex、OpenCode 都消费同一个增强后的 `AgentRequest`。

配置需要进入 V2 config load/save，并包含在 UI config payload 里。建议 JSON 形状：

```json
{
  "audio_asr": {
    "enabled": false,
    "echo_transcript": true,
    "timeout_seconds": 60,
    "endpoint_path": "/v1/audio/transcriptions",
    "model": "qwen3-asr-flash",
    "max_file_bytes": null
  }
}
```

### 5. 消息格式

转写结果追加到用户原始消息之后，再 prepend 当前时间 / 用户信息等 metadata。
原始附件列表保持不变。

推荐区块：

```text
[Audio Transcripts]
- <filename>: <transcript text>
```

如果用户只发语音，没有文字，那么这个区块就是 agent-visible 的主要内容。
之后现有 backend 的附件渲染仍然会追加：

```text
[User Attachments]
- File: /.../voice.m4a (audio/mp4, 327763 bytes)
```

这满足产品要求：转写文本和原始文件附件会一起提供给 Agent。

如果 `audio_asr.echo_transcript` 开启，则 ASR 成功后立刻向 IM 对话里发一条简短回显。这个回显是独立的用户反馈，不需要等 agent request 成功提交。

```text
Voice transcript:
<transcript text>
```

如果一条消息里有多个语音附件，就合并成一条带文件名标签的回显消息。
ASR 超时或失败时不回显。

### 6. 超时和失败行为

60 秒超时应该包住整个 ASR 增强步骤。如果一条消息里有多个音频附件，
可以在同一个 deadline 下并发请求。部分成功是有价值的：成功完成的转写可以追加，失败或超时的文件静默跳过。

失败时：

- 记录文件名、MIME 类型、状态码 / 错误类型、耗时
- warning / error 级别不要记录音频内容或转写文本
- 不追加 `[Attachment Download Errors]`，因为 ASR 失败不是附件下载失败
- 仍然把 `processed_files` 传进 `AgentRequest`

### 7. Web UI 设置页

当前 Messaging 页只有一个大的 `Message Handling` 面板，里面混了很多概念：

- 输入处理：未来的语音转文字
- Agent 上下文：当前时间、用户信息
- 工作反馈：ACK 模式、耗时展示
- 输出格式：快捷回复按钮、Slack 链接预览
- 范围 / 路由入口：允许的群组

如果把 ASR 继续当成一个 row 塞进去，页面会更难扫。建议把页面拆成几个分组面板，
继续复用现有设置组件：`SettingsPanel`、`SettingsRow`、`ToggleSwitch`、`CompactField`、`CompactSelect`，不要重新发明一套视觉系统。

推荐分组：

1. **Input Enrichment / 输入增强**
   - 语音转文字开关。
   - 回显转写结果开关，只在语音转文字开启时可用。
   - 未来可加状态提示：缺少 Vibe Cloud pairing 时展示“需要先配对 Vibe Cloud”。
   - 这个组负责所有“用户内容进入 Agent 前的转换”。

2. **Agent Context / Agent 上下文**
   - 携带当前时间。
   - 携带用户信息。
   - 这两个都是给 Agent 消息 prepend metadata，应该放一起。

3. **Work Feedback / 工作反馈**
   - ACK 模式。
   - 显示耗时。
   - 这些设置影响 Vibe Remote 如何反馈运行中状态、如何处理一次 turn。（原 OpenCode 错误重试次数配置项与自动 `'continue'` 重试后续已移除。）

4. **Reply Experience / 回复体验**
   - 快捷回复按钮。
   - Slack 链接预览抑制，仅 Slack 启用时展示。
   - 这些设置影响 bot 发出的消息，而不是 Agent 输入。

5. **Groups and Routing / 群组与路由**
   - 现有“管理群组 / channel”的入口。
   - 保持为底部导航 row 或小面板，不要和消息转换设置混在一起。

页面 header 里的 autosave 状态可以保留。每个 row 继续即时 autosave，不需要新增 Save 按钮。

语音转文字开关行为：

- Label: "Audio transcription" / "语音转文字"
- Description: "Transcribe supported audio attachments before sending the turn to the agent." / "在把消息交给 Agent 前，先转写支持的语音附件。"
- 如果还没有配对 Vibe Cloud，第一版建议禁用开关并展示短提示：需要先配对 Vibe Cloud。
- 正常 UI 不暴露 model id 或 endpoint path。这些是产品默认值，不是日常用户控制项。

回显转写结果开关行为：

- Label: "Echo transcript" / "回显转写结果"
- Description: "Send the recognized text back to the chat when transcription succeeds." / "语音识别成功后，把转写文本也发回当前对话。"
- 默认开启。
- 语音转文字关闭时禁用。

### 8. 测试

围绕 shared handler / service 边界加聚焦测试：

- 非音频附件不会触发 ASR
- 音频附件转写成功时，会追加 `[Audio Transcripts]`，并保留原文件附件
- ASR 超时时，fallback 到原消息和原附件
- ASR 报错时，fallback 到原消息和原附件
- 多个音频附件允许部分成功
- 未配对 / 缺少后端凭据时跳过 ASR
- 微信已有 `voice_item.text` 时不重复转写，除非同时存在真实音频附件
- 开启回显时，ASR 成功会发回转写结果
- 关闭回显时，ASR 成功也不会发回转写结果

尽量把测试放在 `MessageHandler` 层，用 fake ASR service 注入 controller。
如果 AVIBE endpoint 契约已经定下来，再补 HTTP payload 形状的低层 client 测试。

UI 测试只有在现有 setup 能低成本覆盖 settings page 时才加。否则这个设置页布局改动可以用 `npm run build` 加手动截图检查。

## 实施步骤

1. 在 V2 config load/save 里加入 `AudioAsrConfig`。
2. 新增 `core/audio_asr.py`，包含音频判断、HTTP client、转写文本格式化 helper。
3. 在 `core/controller.py` 里挂载 `AudioAsrService`。
4. 在 `MessageHandler._handle_turn` 中，附件下载完成后、prepend metadata 前调用 ASR service。
5. 把 `audio_asr` 加入 Web UI config payload 和 `SettingsMessagingPage` 的保存 patch。
6. 重组 `SettingsMessagingPage` 为多个分组面板，并把语音转文字和回显开关放到 Input Enrichment。
7. 添加英文和中文 i18n 文案，包括新分组标题、描述、Vibe Cloud pairing 提示。
8. 在 `tests/test_message_handler_attachments.py` 或新的 `tests/test_audio_asr.py` 里加聚焦测试。
9. 跑目标 Python 测试、对修改过的 Python 文件跑 `ruff check`，并对 UI 跑 `npm run build`。
10. 等 AVIBE 后端 endpoint 可用后，用 m4a / ogg fixture 做一次 staging 或 production 的集成 / 手动验证。

## 最终决策

- AVIBE route：`/v1/audio/transcriptions`。
- 鉴权：复用已配对设备身份，使用 `X-Vibe-Instance-Id` 和 `X-Vibe-Device-Secret`。
- 可用性：只给已配对 Vibe Cloud 的用户使用；后端同时检查最近 runtime status，并要求 `tunnel_running=true`。
- 微信 Silk：第一版不支持；有微信官方语音转写文本时继续复用。
- 回显：默认开启，ASR 成功后立刻发。

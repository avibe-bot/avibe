# Telegram 配置指南

## 太长不看

```bash
vibe
```

在向导里选择 **Telegram**，先用 **@BotFather** 创建机器人，粘贴并验证令牌，再完成 Telegram 侧初始化设置。

---

## 第 1 步：用 BotFather 创建机器人

1. 在 Telegram 中打开 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot`
3. 设置机器人的显示名称
4. 设置一个以 `bot` 结尾的用户名
5. 保留生成令牌的页面

如果你已经创建过机器人，可以直接跳到复制令牌这一步。

---

## 第 2 步：在 Avibe 中粘贴令牌

1. 运行 `vibe`
2. 在设置向导中选择 **Telegram**
3. 粘贴从 BotFather 拿到的机器人令牌
4. 点击 **验证令牌**

如果验证失败，先确认复制的是完整令牌，并且没有带多余空格。

---

## 第 3 步：完成 BotFather 里的关键开关

在 **@BotFather** 里依次执行下面几个命令，并且每次都选中你的机器人：

- `/setprivacy`
- `/setjoingroups`
- `/setcommands`

推荐这样设置：

- **`/setprivacy` -> `Disable`**：如果你希望机器人在群里无需 `@` 也能响应普通消息，这一步必须这么设
- **`/setjoingroups` -> `Enable`**：如果机器人需要加入群组或 forum supergroup，这一步必须开启
- **`/setcommands`**：把 `/start`、`/settings`、`/new`、`/resume` 这类常用命令写进去

Telegram 最常见的初始化问题，就是忘了把隐私模式关掉。隐私模式开着时，机器人只能收到命令、@提及和回复消息。

---

## 第 4 步：完成配置、先绑定，再发现群组

Avibe 是通过入站消息来发现 Telegram 会话的。Telegram 没有提供“列出 bot 所在全部群聊”的通用 API。

注意：Telegram 私聊本身可以正常使用，但不会显示在向导里的会话选择列表中。向导只选择已发现的群组和 forum chat；完成设置后，Dashboard 的**群组设置**页面还会列出 Bot 已经见过的 forum topic。

### 单独配置 Forum Topic

打开 **Dashboard → 群组设置 → Telegram**，展开一个 forum 群组。每个已发现的 topic 一开始都会继承群组设置。点击**单独配置**后，可以为该 topic 分别设置：

- 是否启用以及是否要求 `@提及`
- 是否仅允许已绑定用户
- Vibe Agent、模型、推理强度和工作目录
- 要显示的消息类型

点击**恢复群组设置**会删除 topic 覆盖并重新继承群组。Telegram 没有提供完整的 topic 列表 API，因此 Bot 在某个 topic 收到消息后，它才会出现在页面中。General topic 统一按 topic ID `1` 处理。

第一次配置时，请按这个顺序来：

1. 先完成 Avibe 的配置流程，并把服务启动起来
2. 在最后的 Summary 页面复制首个绑定命令
3. 打开和机器人的私聊，发送 `bind <code>`（或 `/bind <code>`），把自己绑定成第一个管理员
4. 绑定完成后，再发送 `/start`，确认私聊链路正常
5. 如果你要在群组或 forum 里用，把机器人加进去，并给它发送消息权限
6. 如果你要用自动建 forum topic，还要给机器人管理员权限或 topic 管理权限
7. 在每个目标群组或 forum chat 里先发一条消息；对 forum 来说，在 forum 内发消息可以帮助 Avibe 发现这个 chat，并在后续启用 topic 相关行为
8. 到 Dashboard 的群组设置页面刷新 Telegram 会话列表，再启用刚发现的目标群组或 forum chat

如果机器人在群里只对命令有反应，先回去检查 `/setprivacy` 是否真的已经设置为 `Disable`。

---

## 第 5 步：选择 Telegram 默认行为

向导里有两个比较重要的 Telegram 默认项：

- **群组中需要显式触发**
  - 开启后，机器人只会响应命令、@提及或回复
  - 适合比较吵的群
- **Forum 自动建 Topic**
  - 对启用了 forum 的 supergroup，新顶层消息可以自动创建一个新 topic
  - 机器人需要有管理员权限或 topic 管理权限

---

## 在 Telegram 里使用

### 私聊

1. 打开你的机器人
2. 先发送 `bind <code>`（或 `/bind <code>`），使用 Avibe 展示的绑定码完成绑定
3. 再发送 `/start`
4. 后面就可以正常聊天了

### 群组

1. 把机器人加进群组
2. 如果开启了 `require_mention`，请用 `/start`、`@botname` 或回复机器人消息的方式触发
3. 如果关闭了 `require_mention`，机器人也可以响应普通群消息

### Forum Topic

1. 把机器人加进启用了 forum 的 supergroup
2. 确保权限足够
3. 在目标 topic 里先发一条消息，让 Avibe 发现它

---

## 故障排查

| 问题 | 解决 |
|------|------|
| 令牌验证失败 | 回到 BotFather 重新复制令牌，再验证一次 |
| 私聊正常，但群里不工作 | 执行 `/setjoingroups`，确认是 `Enable` |
| 群里只对命令或 @ 有反应 | 执行 `/setprivacy`，确认设置成了 `Disable` |
| 向导里看不到目标群组或 forum | 先在目标会话里发一条消息，再刷新 Telegram 会话列表 |
| Forum 自动建 Topic 不生效 | 给机器人管理员权限或 topic 管理权限 |

**日志：** `~/.vibe_remote/logs/vibe_remote.log`

**诊断：** `vibe doctor`

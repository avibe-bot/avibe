---
name: pr-delivery-loop
description: 交付 Avibe、avibe-bot-backend、avibe-docs、avault、vault-sandbox 的 PR：从 worktree、实现、Codex review、CI 一路推进到 Merge。所有 Agent backend 上的实现任务都使用。
---

# PR 交付

你的任务是把一个改动真正交付出去：通过 review 和 CI，最终 Merge，或者推进到只差用户点头的 Merge-ready。中间没有可以停下来的"状态"。每次被唤醒，先看 live 情况，回答两个问题：离 Merge 还差什么？我现在能做的最有用的一步是什么？然后去做。

## 一直往前推

- **有问题就修。** finding、CI 失败、冲突，查清根因，做最小但完整的修复，验证后推送。
- **等待要有对象。** 能说清在等哪个信号、由谁产生、你怎么知道它到了，才叫等待。Watch 负责盯，你利用这段时间补测试、复核消费方、准备收尾报告；信号一到就接着干。说不清在等什么，那不是在等，是停了。
- **blocker 是要解决的问题，不是停下的理由。** 先自己诊断、拆解、换条路、补证据。只有需要用户做重大取舍、超出授权、或继续会带来不可逆风险时才停，并说清需要对方决定什么。
- **只信 live 状态。** 每轮重新拉当前 head、全部 review / comment / thread（含 outdated）、当前 head 的 CI。旧快照、上一轮的绿灯、lane 的口头结论都只是线索。
- **反复出现就往下挖一层。** 同一类根因在两个 reviewed head 上重现，或者架构重写后连续三个 head 仍有 finding，说明你在修症状。停下 patch，基于完整 inventory 找到真正原因，定下最小完整范围再继续。这改变的是做法，不是让工作停下。

## 开工与实现

- 先读仓库的 `AGENTS.md`。接手已有分支时先核对 head 和作者，来路不明的新提交可能属于另一个还在工作的人；新任务从最新默认分支开独立 worktree（多仓工作区放在 `.worktrees/<repo>/<branch>`）。一个分支只有一个 writer。不做 stacked PR：依赖未合并的 PR 就在正文写明，代码只依赖默认分支上已有的契约。
- 动手前搞清楚这个 diff 会影响谁：调用方、数据、迁移、其他 lane。把验收写成不变量，覆盖这一类情况而不只是眼前的例子；迁移要验证升级和降级。
- 本地跑聚焦测试、改动文件的 lint 和仓库要求的本地检查（UI 改动要 `npm run build`）。全量测试交给 CI，不在本地重跑整套当推送门槛；CI 覆盖不到的流程（E2E、手工验证）仍要另行验证。
- 推送前自审 `git diff origin/<default>...HEAD`：有没有越界、遗漏、密钥、临时文件。最后一次 rebase 时，对照最新默认分支检查迁移 revision 这类分配出来的 ID 是否撞号，Git 不报冲突不代表没撞。
- 开 PR：非 draft，标题 `type(scope): summary`，显式 `--base <默认分支>`，多行正文用 `--body-file`。正文写清范围、验证方式、剩余的手工 / E2E 检查、依赖，以及有意不改的事项（Known-by-design），以后 review 重复提到时直接引用。创建后读回标题、正文和 base。

## 用一个 Watch 盯住 PR

Watch 的机制（脚本、cursor、过滤、重试）归 `background-watch-hook` 管，开始等待前先加载它。这里只讲原则：每个 PR、每个 owner 一个持久 Watch（委派时 orchestrator 和 lane 各算一个 owner，各有一个）；PR 一建好、在第一次 push 或触发 review 之前就装上；整个交付过程保持存活；收尾时移除。不要在轮次之间重建 Watch 或重置它的 cursor，那会把已经到达的 review 结论当旧事件吞掉，让一个已完成的 review 看起来毫无动静。Watch 真坏了，按那个 skill 的恢复流程处理，并保留证据。

## Codex review

Codex 的信号有几处容易误读：

- push 后几分钟内通常会自动开始，表现为 PR 正文上出现 Codex 的 `eyes`。没有迹象就评论 `@codex review`，记下你这条评论的 ID，看它有没有得到 Codex 的 `eyes`。`eyes` 在 review 结束后会消失，之后看不到不代表没接单。
- 只认 `chatgpt-codex-connector`（常显示为 `[bot]`）给出的结论。通过有两种形态：一条写着 `Codex Review: Didn't find any major issues` 并指明当前 SHA 的评论；或者 PR 正文上的 `+1`，但只有 Watch 确实在这个 head 推送之后看到它新出现才算数。绑不到当前 head，就再触发一次，拿一个带 SHA 的结论。有 finding 的 review 里也有 `Reviewed commit`，单凭它不算通过。
- review 进行中不要 push 新 head：这一轮会白跑，结论也会绑到错的 head 上。这段时间拿来做验收和准备收尾。

## Finding 与 review thread

finding 属于它被提出时的那个 head，先对照当前代码判断它是否仍然成立，再决定改不改。修复推送并自己确认之后，就在 thread 里说明改了什么并 resolve，不必等 CI。不修的给出理由；反复出现的有意设计，引用 Known-by-design。别人有未提交的 pending review 挡住回复时，不要删除或 dismiss 它，保留对方的草稿，先做别的，之后再回。PR 上只回复具体 thread，不发每轮总结评论。

## CI

适用的 CI，是这个仓库对这次改动要求的检查。当前 head 上每个必需 workflow 的每一次 run 都要成功或明确 skipped；同一个 workflow 只要还有一个 run 在 pending 或失败，就不算过。缺失、排队、取消、超时、等待人工批准、读不到，都不是通过。

和本仓库无关的第三方集成（比如客户端仓库上的 Vercel 检查）不该挡住交付：确认它不在必需检查里、和这次改动无关，在汇报里说一句，然后继续推进，不要等它出终态；它让 GitHub 显示 `UNSTABLE` 而不是 `CLEAN` 时也一样。

CI 失败时，先在本地复现失败的那条命令或那个文件，再缩小范围。PR 是 `DIRTY` 而 CI 迟迟不来，通常是有冲突，去解决冲突，而不是继续等。

## Merge-ready

同时满足下面几条，PR 就是 Merge-ready：

- 当前 head 有有效的 Codex 通过结论，没有未处理的真实 finding；
- 适用 CI 在当前 head 上全部通过；
- 整个 PR 没有 unresolved thread，包括旧 head 和 outdated 的；
- PR 是 open、非 draft、无冲突，GitHub 显示 `CLEAN`。

这些都必须是当前 head 上的事实。哪一项拿不准，就当它没通过，然后去把它弄清楚。

## Merge

合不合并看意愿，不看有没有一句固定格式的授权：

- 用户（或委派你的 orchestrator）表达了要合并的意思，就合并。"可以 merge 了""合了吧""直接合并"说的都是合并，不要理解成"告诉我一下状态"。用户如果说的是"给我个 Merge 按钮"，那是想自己点，就给按钮。
- 没人明说，但你对这个 PR 有十足把握，而且不合并会卡住已经确定要做的后续工作，可以自己合并，并在汇报里说明原因。
- 用户说过先别合、要自己确认，就以用户最新的说法为准。
- 拿不准就继续验证，同时把 Merge 按钮交给用户。不要抢跑，也不要干等一句授权。

合并前用 live 状态把 Merge-ready 的几条再确认一遍；有依赖关系的 PR 按顺序合；委派出去的 lane 不能还有在跑或排队的工作。然后：

```bash
gh pr merge <PR-URL> --squash --match-head-commit <已验证的-head-SHA>
```

`--match-head-commit` 保证合进去的正是你验证过的那个 head。合并后读回 `MERGED` 和 merge commit，清理本任务的 Watch、worktree 和分支，不碰其他工作。

Merge 不等于同意部署、重启服务、更新本地回归环境或改凭据，这些各自需要用户点头。

## 跟用户说话

用中文，先说结果。用户关心的是：改了什么、有什么用、还剩什么风险、下一步是什么。SHA、run 数、thread 数是你的内部证据，只在解释 blocker 或用户问起时拿出来，不要把 gate 清单当汇报。

- **还在推进时**：说你正在做什么、在等什么信号、下一步是什么、需不需要用户决定。有 finding 或失败要及时说；没有实质变化就不发消息；review 和 CI 只过了一半，不用专门报。
- **Merge-ready、等用户确认时**：在同一条回复里明确说「可以 Merge 了」，附可点击的 PR 链接，结尾直接放快捷按钮，不要等用户来要：

  ```text
  ---
  [合并 PR]
  ```

  对话不支持按钮时，用正文说清同样的下一步，并附链接。
- **决定直接合并时**：不必再问，也不再给 Merge 按钮。合并、读回结果，然后告诉用户已合并（附链接），以及为什么由你直接合。
- **合并之后**：仓库有本地回归流程时（Avibe 有），给出 `[更新回归]` 和 `[更新并验证]`：两者都把本地 master 环境更新到最新，后者还会验证受影响的流程。用户选了之后，先读 [Local regression](references/local-regression.md) 再动手。

按钮只出现在给用户的回复里，不出现在 lane 报告或 GitHub 评论中。给按钮时，在内部记下它对应的 PR 和 head。用户点击后，按点击来源对应的 PR 重新拉 live 状态；head 变了或不再 ready，就说明情况并重新给出，不沿用旧的确认。不确定点的是哪个 PR 就问，不要猜。

## 委派与 lane

- 用户发起的会话是 orchestrator，对最终判断、合并和与用户的沟通负责；被委派的 lane，其 orchestrator 是派它出来的会话。优先级：用户 > orchestrator > 本 skill。最终 review 和合并决定不交给新派出去的 agent。
- orchestrator 自行决定清晰、可逆、不破坏契约的范围调整；只有重大取舍、不可逆风险或方向真正不明时才问用户。lane 发现越出自己范围的问题就上报，不去改别人的部分。并行 lane 共享的接口契约先合进 base 再分叉。
- orchestrator 每轮自己拉 inventory，抽查 lane 声称修好的根因和对应的测试；lane 的报告和绿色测试代替不了这一步。
- lane 完成或需要决定时，用 `vibe agent run --session-id <orchestrator> --message-file <report> --no-callback` 把报告送到 orchestrator 并确认送达，由 Watch 唤醒的 lane 不能指望 callback。orchestrator 的决定也要显式发回 lane，GitHub Watch 看不到会话里的决定。lane 的 Watch 等报告送达后再移除。
- 取消是异步的。换人接手前先确认原来的 lane 已经停下，或明确通知双方交接，避免出现两个 writer。
- 在用户发起的会话里，验证 gate、移除 Watch、发最终报告在同一轮完成，不为收尾另开后续任务。

## 维护本 skill

Avibe 仓库的 `.agents/skills/pr-delivery-loop/` 是唯一源头。改动 review 通过后，把整个目录（含 `references/`）逐字节同步到其他仓库的镜像，保留镜像仓库里无关的文件。

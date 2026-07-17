# 云团子 / Vibey — 原始 Codex CLI prompts

按时间顺序的 4 次调用：

1. **p1_initial_3candidates.md** — 第一次尝试，生成云/雾/烟三种候选形态。让 Codex 用 OpenAI `gpt-image-1` API 直接出图。
2. **p2_retry_builtin_imagegen.md** — 改用 Codex 内置 `image_gen` 工具重试（避免 CLI fallback 的麻烦）。
3. **p3_edit_more_colorful_glow.md** — 选定云团子后，做一次局部 edit：让内部发光点更彩色（产出 cloud-tuanzi-v2）。
4. **p4_full_9image_set.md** — 基于 v2 出全套：6 表情 + 2 Logo + 1 深色版。

调用方式（每次都是 stdin 喂 prompt，否则 codex exec 会卡）：

```bash
cat assets/mascot/prompts/P4_full_9image_set.md \
  | codex exec --json \
      --dangerously-bypass-approvals-and-sandbox \
      --skip-git-repo-check \
      --cd . -
```

# P1 — initial 3-candidate generation (used OpenAI gpt-image-1 API path)
# Codex session: 13:42:50 (rollout-2026-05-03T13-42-50-019dec5c-625b-7962-9cd5-6af845ef0ba0.jsonl)

请帮我生成三张吉祥物候选形象图,用于一个叫 Vibe Remote 的 AI 助手产品。

## 背景
- Vibe Remote 是把 AI agent 接入 Slack/Discord/Telegram/飞书/微信等 IM 的中间件
- 我们想给它一个可爱的拟人吉祥物,中文叫'团子',英文叫 Vibey
- 灵感来自《Rick and Morty》第二季 Mortynight Run 里的气体生物 Fart(浅霓虹蓝半透明云,内部 5 个彩色发光球),但要做成完全原创设计来规避版权,不能用浅霓虹蓝+5彩球这种识别点

## 通用设计要求
- 一团有意识的气态生物,圆滚滚一团,半透明 + 微微发光
- 有一对可爱的圆眼睛(像黑豆眼或宫崎骏煤炭精灵那种简单的圆点眼),嘴巴可有可无,要表达温和友好
- 内部隐约有 3-5 个发光小光点(代表它接入的多个 IM 平台,但不要明显的 5 彩球布局,可以是模糊的内部光晕)
- 卡通治愈系扁平风格,边缘柔和,适合做产品 logo 和吉祥物
- 干净的白色或非常浅的渐变背景
- 1024x1024 方形构图,主体居中

## 三张图各自的特点(请生成 3 张独立的图)

### 1. cloud-tuanzi.png(云团子)
- 白色蓬松、像棉花糖云一样饱满圆润
- 边缘比较干净规整,但保留云朵的卷曲感
- 整体颜色:纯白带一点点淡蓝或淡粉的高光
- 形状最圆、最饱满,最像一只圆滚滚的小球

### 2. mist-tuanzi.png(雾团子)
- 灰白色或淡青色雾气感
- 边缘更朦胧、不规则、有飘散感
- 整体偏冷色调,带一丝神秘
- 形状不那么规整,有一点拉长或不对称

### 3. smoke-tuanzi.png(烟团子)
- 浅米白色或浅米色,轻烟感
- 有向上飘起的尾迹或丝状结构
- 灵动、轻盈,比云团子更纤细
- 整体暖色调,但很淡

## 输出
- 用 OpenAI 的 gpt-image-1 模型(images.generate API)生成
- 保存到仓库的 `assets/mascot/` 目录下,文件名分别是 cloud-tuanzi.png, mist-tuanzi.png, smoke-tuanzi.png
- 完成后告诉我三张图的仓库相对路径

如果 OPENAI_API_KEY 没有设置,请先检查环境(env | grep OPENAI 或 echo $OPENAI_API_KEY),并报告问题。如果 API key 可用就直接生成。

可以用 Python(openai SDK)或 curl 直接调用 API,你选最快的方式。

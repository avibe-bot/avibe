// Setup · intro: the first screen of `/setup`, the shipped Wizard as a fresh install opens it. The
// story shows the assistants relaying one task; the six ways in to Avibe stay folded for the journey.
import { Wizard } from '@/components/Wizard';

import { frames, type FrameSpec } from '../../frame';
import { setupFixtures, type SetupSpec } from './fixtures';

const route = '/setup';
const wizard = () => <Wizard />;
/** Milliseconds into the story's relay a frame is drawn at. */
const relay = 4700;

const screen = (name: string, title: string, spec: SetupSpec = {}, extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped',
  route,
  fixtures: setupFixtures(spec),
  render: wizard,
  ...extra,
});

const designFrames = frames([
  // The story plays on its own clock; 5 s in, Vibey and Claude Code are done and Codex is reviewing.
  screen('default', '设置向导 · 介绍 · 各有所长，接力完成（Codex 评审中）', {}, { wait: relay }),
  screen('short', '设置向导 · 介绍 · 矮窗口 1366×768', {}, { width: 1366, height: 768, wait: relay }),
  screen('large', '设置向导 · 介绍 · 大屏 1920×1080', {}, { width: 1920, height: 1080, wait: relay }),
  screen('phone', '设置向导 · 介绍 · 手机（操作栏固定在底部）', {}, { width: 390, height: 844, wait: relay }),
  screen('gateway-required', '设置向导 · 介绍 · 模型网关已关闭，需恢复后重试', { gatewayOff: true }),
  screen('read-failed', '设置向导 · 介绍 · 读取配置失败', { api: { '/api/config': { ok: false, error: 'config.json: unexpected end of JSON input' } } }),
]);

export default designFrames;

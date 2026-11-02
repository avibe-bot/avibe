// Feedback: the toast and the error fallback.
//
// ToastProvider draws its toasts itself (bottom right, above dialogs) and has no view of its own, so a
// frame raises one the way the product does: it opens a shipped control and clicks it. Here that is
// the backend lifecycle chip's 重启, answered from fixtures. ErrorFallback is what an error boundary
// shows, on a page or in place.
import { BackendLifecycleChip } from '@/components/settings/BackendLifecycleChip';
import { ErrorFallback } from '@/components/ui/error-fallback';

import { frames } from '../../frame';
import { RUNTIMES } from '../models/backends.fixtures';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const noop = () => {};

const restartHost = () => (
  <div className="flex justify-end p-6">
    <BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath="codex" detectedRuntime={RUNTIMES.codex} />
  </div>
);

const restart = (result: { ok: boolean; message: string }) =>
  systemFixtures({ '/api/backend/codex/runtime': RUNTIMES.codex, '/api/backend/codex/restart': result });

const restartClicks = ['role=button[name="{t:backendLifecycle.statusReady}"]', 'role=button[name="{t:backendLifecycle.restart}"]'];

const fallbacks = () => (
  <Sheet>
    <Section title="ErrorFallback" note="错误边界接住异常后显示">
      <Specimen name='variant="inline"' use="页面里的一块出错" className="w-[26rem]">
        <div className="h-72 overflow-hidden rounded-xl border border-border">
          <ErrorFallback variant="inline" reset={noop} error={new Error("Cannot read properties of undefined (reading 'turns')")} />
        </div>
      </Specimen>
      <Specimen name="模块加载失败" use="页面开着时 Avibe 已更新，只能重新加载" className="w-[26rem]">
        <div className="h-72 overflow-hidden rounded-xl border border-border">
          <ErrorFallback variant="inline" reset={noop} error={new Error('Failed to fetch dynamically imported module: /assets/ChatPage-3f2a.js')} />
        </div>
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  {
    name: 'toast-success',
    title: '反馈 · 提示条（成功）',
    width: 800,
    height: 560,
    status: 'shipped',
    fixtures: restart({ ok: true, message: '' }),
    click: restartClicks,
    render: restartHost,
  },
  {
    name: 'toast-error',
    title: '反馈 · 提示条（失败）',
    width: 800,
    height: 560,
    status: 'shipped',
    fixtures: restart({ ok: false, message: 'Codex app-server 没能启动：端口 4500 已被占用' }),
    click: restartClicks,
    render: restartHost,
  },
  { name: 'error-fallback', title: '反馈 · 出错兜底', width: 1000, height: 420, status: 'shipped', fixtures: systemFixtures(), render: fallbacks },
]);

export default designFrames;

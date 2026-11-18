/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

// ui/design: mobile-app/direct-path. Each case loads fresh modules, React and
// i18n included, as a new document would: the direct tracker belongs to the
// document.
type Fresh = {
  act: typeof import('@testing-library/react').act;
  screen: typeof import('@testing-library/react').screen;
};
let fresh: Fresh;
const settle = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

const installShell = (capabilities: Record<string, number>) => {
  const pending: Array<(value: unknown) => void> = [];
  const call = vi.fn((method: string) => new Promise((resolve) => {
    if (method === 'direct.status') pending.push(resolve);
    else resolve(null);
  }));
  let deliver: (payload: unknown) => void = () => {};
  const on = vi.fn((event: string, handler: (payload: unknown) => void) => {
    if (event === 'direct.transportChanged') deliver = handler;
    return () => undefined;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform: 'ios', shellVersion: '1.0 (1)', capabilities, call, on },
  });
  return {
    call,
    answer: async (value: unknown) => {
      await fresh.act(async () => {
        pending.shift()?.(value);
        await settle();
      });
    },
    emit: (payload: unknown) => fresh.act(() => deliver(payload)),
  };
};

const renderLine = async (lang: 'en' | 'zh' = 'en') => {
  vi.resetModules();
  const rtl = await import('@testing-library/react');
  const { default: i18n } = await import('../i18n');
  await i18n.changeLanguage(lang);
  const { DirectPathLine } = await import('./DirectPathLine');
  fresh = { act: rtl.act, screen: rtl.screen };
  return rtl.render(<DirectPathLine />);
};

afterEach(async () => {
  Reflect.deleteProperty(window, 'avibeNative');
  const { cleanup } = await import('@testing-library/react');
  cleanup();
});

describe('DirectPathLine', () => {
  it('renders nothing and reads nothing without the direct capability', async () => {
    const shell = installShell({ app: 1, instancePresentation: 1 });
    const { container } = await renderLine();
    await fresh.act(settle);
    expect(container.innerHTML).toBe('');
    expect(shell.call).not.toHaveBeenCalledWith('direct.status');
  });

  it('renders nothing outside the App', async () => {
    const { container } = await renderLine();
    expect(container.innerHTML).toBe('');
  });

  it('shows the path new connections try, and follows newer events only', async () => {
    const shell = installShell({ app: 1, direct: 1 });
    const { container } = await renderLine();
    expect(container.innerHTML).toBe('');

    await shell.answer({ preferred: 'tunnel', sequence: 1, closure: 0 });
    expect(fresh.screen.getByTestId('direct-path-line').textContent).toBe('New connections try: Cloudflare');

    shell.emit({ preferred: 'direct', sequence: 2, closure: 0 });
    expect(fresh.screen.getByTestId('direct-path-line').textContent).toBe('New connections try: direct');

    // An older preference is ignored; a closure still counts.
    shell.emit({ preferred: 'tunnel', sequence: 1, closure: 2 });
    expect(fresh.screen.getByTestId('direct-path-line').textContent).toBe('New connections try: direct · 2 drops');
  });

  it('speaks Chinese', async () => {
    const shell = installShell({ app: 1, direct: 1 });
    await renderLine('zh');
    await shell.answer({ preferred: 'direct', sequence: 3, closure: 1 });
    expect(fresh.screen.getByTestId('direct-path-line').textContent).toBe('新连接优先走：直连 · 路径中断 1 次');
  });
});

// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';

import { desktopEn as en, desktopZh as zh } from '../i18n/desktop';
import type { DesktopClientState } from './bridge';
import { DesktopSignIn } from './DesktopSignIn';

const signedOut: DesktopClientState = {
  version: 1,
  revision: 1,
  generation: 1,
  account: { status: 'signed_out', identity: null, email: null, gate: null, methods: [] },
  connection: { status: 'local', committed: { kind: 'local', name: 'Studio', instance_id: null }, pending: null },
  enrollment: { status: 'unpaired', instance_id: null, error_code: null },
  catalog: { status: 'idle', instances: [] },
  local: { name: 'Studio', remote_access: 'off' },
  desktop: { version: '3.2.7', update: { phase: 'idle', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
  consent: null,
  remote_auxiliary: 'local',
  instance_update: null,
};

afterEach(() => cleanup());

// ui/design: desktop/sign-in/start. Max, 2026-11-20 10:13/10:14.
it.each([['en', en], ['zh', zh]] as const)(
  'AUTH-SETUP-182 the first page is the iPhone Welcome with the Desktop subtitle and no registration note (%s)',
  async (lng, copy) => {
    const i18n = createInstance();
    await i18n.init({ lng, resources: { [lng]: { translation: copy } } });
    const view = render(
      <I18nextProvider i18n={i18n}>
        <DesktopSignIn
          state={signedOut}
          send={vi.fn(async () => null)}
          onLocal={() => undefined}
          onDifferentAccount={() => undefined}
          onOpenSecurity={() => undefined}
          renderEnroll={() => null}
        />
      </I18nextProvider>,
    );
    const page = view.container.querySelector('[data-account-login]')!;
    const welcome = copy.mobileApp.welcome;
    // The App's hero, title and footer, word for word.
    expect(screen.getByAltText(welcome.mascotAlt)).toBeTruthy();
    expect(screen.getByText(welcome.greeting)).toBeTruthy();
    expect(screen.getByRole('heading').textContent).toBe(`${welcome.titleLine1}${welcome.titleLine2}`);
    expect(page.textContent).toContain(`${welcome.footerLine1}${welcome.footerLine2}`);
    // The Desktop's own subtitle, not the App's.
    expect(page.textContent).toContain(`${copy.desktopAccount.welcomeLine1}${copy.desktopAccount.welcomeLine2}`);
    expect(page.textContent).not.toContain(welcome.bodyLine1);
    // No brand row; "continue on this computer" stays.
    expect(page.textContent).not.toContain(copy.mobileApp.brand.agentOs);
    expect(screen.getByRole('button', { name: copy.desktopAccount.continueLocal })).toBeTruthy();
    expect(screen.getByRole('button', { name: copy.mobileApp.welcome.continueWithEmail })).toBeTruthy();
  },
);

// Max, 2026-11-20 10:53: a later step's own way back reads "Previous step", apart from the header's
// "Back to Workbench"; the iPhone keeps its "Back".
it.each([['en', en], ['zh', zh]] as const)('AUTH-SETUP-182 a later step goes back with "Previous step" (%s)', async (lng, copy) => {
  const i18n = createInstance();
  await i18n.init({ lng, resources: { [lng]: { translation: copy } } });
  render(
    <I18nextProvider i18n={i18n}>
      <DesktopSignIn
        state={signedOut}
        send={vi.fn(async () => null)}
        onLocal={() => undefined}
        onDifferentAccount={() => undefined}
        onOpenSecurity={() => undefined}
        renderEnroll={() => null}
      />
    </I18nextProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: copy.mobileApp.welcome.continueWithEmail }));
  expect(screen.getByLabelText(copy.mobileApp.email.label)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: copy.desktopAccount.previousStep }));
  expect(screen.getByRole('button', { name: copy.mobileApp.welcome.continueWithEmail })).toBeTruthy();
  expect(screen.queryByRole('button', { name: copy.common.back })).toBeNull();
});

// The browser's last page after a provider sign-in speaks this page's language (Codex, #4034).
it.each([['en', en], ['zh', zh]] as const)('AUTH-SETUP-183 a provider sign-in names this page\'s language (%s)', async (lng, copy) => {
  const i18n = createInstance();
  await i18n.init({ lng, resources: { [lng]: { translation: copy } } });
  const send = vi.fn(async () => null);
  render(
    <I18nextProvider i18n={i18n}>
      <DesktopSignIn
        state={signedOut}
        send={send}
        methods={{ providers: ['google', 'github', 'apple'], passkey: false }}
        onLocal={() => undefined}
        onDifferentAccount={() => undefined}
        onOpenSecurity={() => undefined}
        renderEnroll={() => null}
      />
    </I18nextProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: copy.mobileApp.welcome.continueWithGitHub }));
  expect(send).toHaveBeenCalledWith({ type: 'sign_in_with_provider', provider: 'github', locale: lng });
});

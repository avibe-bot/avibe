// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { isTouchCapableDevice } from '@/lib/softKeyboard';

import { SignInChrome, type SignInChromeValue } from './chromeContext';
import { OtpCells } from './screens';

vi.mock('@/lib/softKeyboard', () => ({ isTouchCapableDevice: vi.fn(() => false) }));

afterEach(() => {
  cleanup();
  vi.mocked(isTouchCapableDevice).mockReturnValue(false);
});

const Frame: SignInChromeValue['Frame'] = ({ children }) => children;

function CodeField({
  platform = 'desktop',
  busy = false,
  invalid = false,
}: {
  platform?: SignInChromeValue['platform'];
  busy?: boolean;
  invalid?: boolean;
}) {
  const [code, setCode] = useState('');
  return (
    <SignInChrome value={{ Frame, platform }}>
      <OtpCells
        label="Verification code"
        code={code}
        busy={busy}
        invalid={invalid}
        onChange={setCode}
        onFocus={() => undefined}
      />
      <button type="button">Elsewhere</button>
    </SignInChrome>
  );
}

const field = () => screen.getByRole<HTMLInputElement>('textbox', { name: 'Verification code' });

describe('AUTH-SETUP-182 code entry focus', () => {
  it.each([
    ['desktop', false],
    ['desktop', true],
    ['ios', false], // The shared page in a non-touch browser.
  ] as const)('focuses %s code entry with touch=%s, accepts paste, and stays editable after clearing', async (platform, touch) => {
    vi.mocked(isTouchCapableDevice).mockReturnValue(touch);
    const user = userEvent.setup();
    render(<CodeField platform={platform} />);
    expect(document.activeElement).toBe(field());
    await user.paste('123456');
    expect(field().value).toBe('123456');
    await user.keyboard('{Control>}a{/Control}{Backspace}');
    expect(field().value).toBe('');
    expect(document.activeElement).toBe(field());
    await user.keyboard('7');
    expect(field().value).toBe('7');
  });

  it('restores focus after a busy verification fails without stealing it on ordinary rerenders', async () => {
    const user = userEvent.setup();
    const view = render(<CodeField />);
    await user.paste('123456');
    view.rerender(<CodeField busy />);
    expect(field().disabled).toBe(true);
    // jsdom keeps the disabled input active; model the browser's lost focus.
    screen.getByRole('button', { name: 'Elsewhere' }).focus();
    expect(document.activeElement).not.toBe(field());
    view.rerender(<CodeField invalid />);
    expect(field().disabled).toBe(false);
    expect(document.activeElement).toBe(field());
    await user.keyboard('{Control>}a{/Control}');
    await user.paste('654321');
    expect(field().value).toBe('654321');
    await user.click(screen.getByRole('button', { name: 'Elsewhere' }));
    view.rerender(<CodeField />);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Elsewhere' }));
  });

  it('waits for an initially busy step to unlock before focusing', () => {
    const view = render(<CodeField busy />);
    expect(field().disabled).toBe(true);
    expect(document.activeElement).not.toBe(field());
    view.rerender(<CodeField />);
    expect(document.activeElement).toBe(field());
  });

  it('does not open the iOS/touch keyboard on entry or retry, but still accepts a tap and paste', async () => {
    vi.mocked(isTouchCapableDevice).mockReturnValue(true);
    const user = userEvent.setup();
    const view = render(<CodeField platform="ios" />);
    expect(document.activeElement).not.toBe(field());
    await user.click(field());
    await user.paste('123456');
    expect(field().value).toBe('123456');
    view.rerender(<CodeField platform="ios" busy />);
    screen.getByRole('button', { name: 'Elsewhere' }).focus();
    expect(document.activeElement).not.toBe(field());
    view.rerender(<CodeField platform="ios" invalid />);
    expect(document.activeElement).not.toBe(field());
  });
});

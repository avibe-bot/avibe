/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { SetupRemoteAccessView } from './SetupRemoteAccessView';

// The Setup header's language switcher reads the API; the step's own decisions do not.
vi.mock('../Wizard', () => ({ SetupHeader: () => null }));

// ui/design: setup/remote-access/*
beforeEach(async () => {
  await i18n.changeLanguage('zh');
});
afterEach(cleanup);

describe('SetupRemoteAccessView', () => {
  it('turns on, signs in first, or skips; a taken name blocks turning on', () => {
    const onSkip = vi.fn();
    const onSignIn = vi.fn();
    const { rerender } = render(<SetupRemoteAccessView surface="desktop" account={null} name="" onSignIn={onSignIn} onSkip={onSkip} />);
    fireEvent.click(screen.getByRole('button', { name: '登录并开启' }));
    expect(onSignIn).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '以后再说，进入工作台' }));
    expect(onSkip).toHaveBeenCalledTimes(1);

    rerender(<SetupRemoteAccessView surface="desktop" account="max@example.test" name="max" nameState="taken" />);
    expect((screen.getByRole('button', { name: '开启远程访问' }) as HTMLButtonElement).disabled).toBe(true);
    rerender(<SetupRemoteAccessView surface="browser" />);
    expect((screen.getByRole('button', { name: '开启远程访问' }) as HTMLButtonElement).disabled).toBe(false);
  });
});

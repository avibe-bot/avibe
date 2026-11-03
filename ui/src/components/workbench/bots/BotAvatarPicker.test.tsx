/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BotAvatarPicker } from './BotAvatarPicker';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en' } }),
}));

// The full set stands in for the real one (BotEmojiCatalog.test.tsx): here what
// matters is when it loads and what a pick from it does.
const catalogLoaded = vi.hoisted(() => vi.fn());
vi.mock('./BotEmojiCatalog', () => {
  catalogLoaded();
  return {
    default: ({ onPick, onBack }: { onPick: (emoji: string) => void; onBack?: () => void }) => (
      <div data-testid="all-emoji">
        <button type="button" onClick={() => onPick('🦊')}>fox</button>
        {onBack && <button type="button" onClick={onBack}>back</button>}
      </div>
    ),
  };
});

const phone = (matches: boolean) =>
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches, addEventListener() {}, removeEventListener() {} })));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('BotAvatarPicker', () => {
  it('loads every emoji only from "More", and a pick from it keeps the hue and shows on "More"', async () => {
    phone(false);
    const onPick = vi.fn();
    render(<BotAvatarPicker current={{ emoji: '🛡️', hue: 150 }} onPick={onPick} canUseDefault onError={() => {}} />);

    fireEvent.click(screen.getByText('🧭'));
    expect(onPick).toHaveBeenLastCalledWith({ kind: 'emoji', emoji: '🧭', hue: 150 });
    expect(catalogLoaded).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'bots.avatarPicker.more' }));
    fireEvent.click(await screen.findByText('fox'));
    expect(catalogLoaded).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenLastCalledWith({ kind: 'emoji', emoji: '🦊', hue: 150 });

    // Back on the leading set, which has no fox: "More" is where it shows as chosen.
    const leading = within(screen.getByRole('group', { name: 'bots.avatarPicker.emoji' }));
    expect(leading.queryAllByRole('button', { pressed: true })).toEqual([]);
    expect(leading.getByRole('button', { name: 'bots.avatarPicker.moreChosen' }).textContent).toContain('🦊');
  });

  it('opens every emoji as a sheet on a phone', async () => {
    phone(true);
    const onPick = vi.fn();
    render(<BotAvatarPicker current={null} onPick={onPick} canUseDefault={false} onError={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: 'bots.avatarPicker.more' }));
    const sheet = await screen.findByRole('dialog', { name: 'bots.avatarPicker.more' });
    // No way back but closing: the sheet is the full set alone.
    expect(within(sheet).queryByText('back')).toBeNull();
    fireEvent.click(within(sheet).getByText('fox'));
    expect(onPick).toHaveBeenLastCalledWith({ kind: 'emoji', emoji: '🦊', hue: 200 });
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

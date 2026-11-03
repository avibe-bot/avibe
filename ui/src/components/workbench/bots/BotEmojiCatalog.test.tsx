/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import catalog from 'virtual:emoji-catalog';

import { BOT_AVATAR_EMOJIS } from '@/lib/bots';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh-CN' } }),
}));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

// jsdom lays nothing out; give every row and the viewport a height so the
// virtualized list renders its first rows.
const clientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight');
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 36 });
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  if (clientHeight) Object.defineProperty(HTMLElement.prototype, 'clientHeight', clientHeight);
  vi.restoreAllMocks();
  vi.resetModules();
});

// The emoji the newest Emoji version in the catalog added, and the Chinese name of one
// that no other emoji's names or keywords contain, so a search for it finds only it.
const newestVersion = Math.max(...catalog.map(([, , version]) => version));
const newest = new Set(catalog.filter(([, , version]) => version === newestVersion).map(([emoji]) => emoji));
const newestName = catalog
  .filter(([emoji]) => newest.has(emoji))
  .map(([, , , , , zh]) => zh[0])
  .find((name) => catalog.filter(([, , , , en, zh]) => [...en, ...zh].some((text) => text.includes(name))).length === 1)!;

/** A system whose emoji font draws as color glyphs everything except what `lacks` says. */
function systemDrawing(lacks: (emoji: string) => boolean) {
  const context = {
    canvas: { width: 0, height: 0 },
    font: '',
    textBaseline: '',
    fillStyle: '',
    measureText: (text: string) => ({ width: lacks(text) ? 4 : 2 }),
    clearRect: () => {},
    fillText: () => {},
    getImageData: () => ({ data: new Uint8ClampedArray(16).fill(200) }),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D);
}

async function renderCatalog(onPick = vi.fn()) {
  const { default: BotEmojiCatalog } = await import('./BotEmojiCatalog');
  render(<BotEmojiCatalog current={null} onPick={onPick} columns={8} />);
  return onPick;
}

const search = (text: string) => fireEvent.change(screen.getByRole('searchbox'), { target: { value: text } });

describe('the emoji catalog', () => {
  it('holds every emoji the server accepts, the leading set included', () => {
    // The server's one-emoji cap (core/bots.py), in code points, as Python counts them.
    const cap = Number(/^EMOJI_MAX_LENGTH = (\d+)$/m.exec(readFileSync(resolve(process.cwd(), '../core/bots.py'), 'utf8'))?.[1]);
    const offered = catalog.flatMap(([emoji, , , , , , skins]) => [emoji, ...(skins ?? [])]);
    expect(offered.length).toBeGreaterThan(3500);
    for (const emoji of offered) {
      expect([...emoji].length, emoji).toBeLessThanOrEqual(cap);
      expect(emoji, emoji).not.toMatch(/[\s\p{Cc}]/u);
    }
    // Picked from either surface, the same emoji is the same string.
    const known = new Set(catalog.map(([emoji]) => emoji));
    expect(BOT_AVATAR_EMOJIS.filter((emoji) => !known.has(emoji))).toEqual([]);
    // Skin-tone and hair swatches are not emoji on their own.
    expect(known.has('🏽')).toBe(false);
  });
});

describe('BotEmojiCatalog', () => {
  it('finds an emoji by its Chinese or English name, picks it, and lists it first next time', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const onPick = await renderCatalog();

    search('狐狸');
    fireEvent.click(await screen.findByRole('gridcell', { name: '狐狸' }));
    expect(onPick).toHaveBeenCalledWith('🦊');

    search('octopus');
    fireEvent.click(await screen.findByRole('gridcell', { name: '章鱼' }));
    expect(onPick).toHaveBeenLastCalledWith('🐙');

    cleanup();
    await renderCatalog();
    const recent = screen.getByRole('group', { name: 'bots.emojiPicker.recent' });
    expect([...recent.querySelectorAll('button')].map((button) => button.textContent)).toEqual(['🐙', '🦊']);
  });

  it.each([
    ['an emoji newer than the system draws', (emoji: string) => newest.has(emoji), newestName, true],
    ['a flag the system spells as letters', (emoji: string) => /\p{RI}/u.test(emoji), '旗: 中国', true],
    ['an emoji the system draws', () => false, '狐狸', false],
  ])('leaves out %s', async (_case, lacks, name, missing) => {
    systemDrawing(lacks);
    await renderCatalog();
    search(name);
    if (missing) {
      expect(await screen.findByText('bots.emojiPicker.empty')).toBeTruthy();
    } else {
      expect(await screen.findByRole('gridcell', { name })).toBeTruthy();
    }
  });
});

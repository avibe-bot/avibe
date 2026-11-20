// @vitest-environment jsdom
// What an account's quota card says about its reset cards (ui/design: models/quota-reset-cards/*):
// which rows it lists, the vendor's statement or nothing, why a card cannot be used now, and
// what the server's lock and the page's own step leave on screen. The page's requests are
// covered in SettingsModelsPage.render.test.tsx.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { readyRegion } from './regionRead';
import { QuotaTab } from './QuotaTab';
import type { ResetCardIntent, ResetCardPhase } from './ResetCards';
import type { QuotaWindow, ResetCard, ResetCards, SourceQuota } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const NOW = Date.parse('2026-11-20T03:00:00Z');
const MINUTE = 60_000;
const iso = (ms: number) => new Date(ms).toISOString();

const limit: QuotaWindow = {
  id: 'five_hour', kind: 'session', label: 'five_hour', used_pct: 100, window_seconds: 18_000, resets_at: iso(NOW + 130 * MINUTE),
};
const card = (over: Partial<ResetCard> & Pick<ResetCard, 'kind'>): ResetCard => ({
  count: 1, expires_at: '2026-11-28T09:00:00Z', stated: true, usable: true, blocked: null, blocked_until: null, ...over,
});
const held = (cards: ResetCard[], over: Partial<ResetCards> = {}): ResetCards => ({ state: 'ok', lock: null, cards, ...over });
const account = (source_id: string, vendor: string, reset_cards?: ResetCards): SourceQuota => ({
  source_id, vendor, display_name: source_id, account_label: null, plan: null,
  fetched_at: iso(NOW - MINUTE), state: 'ok', windows: [limit], ...(reset_cards ? { reset_cards } : {}),
});

const draw = (sources: SourceQuota[], over: {
  phases?: Record<string, ResetCardPhase>;
  onIntent?: (sourceId: string, intent: ResetCardIntent) => void;
  canUse?: boolean;
} = {}) => render(
  <I18nextProvider i18n={i18n}>
    <QuotaTab
      quota={readyRegion({ refresh_interval_seconds: 300, sources })}
      now={NOW}
      resetCardPhases={over.phases}
      onResetCardIntent={over.onIntent}
      canUseResetCards={over.canUse ?? true}
    />
  </I18nextProvider>,
);

const cardOf = (name: string) => screen.getByRole('article', { name });
const block = (name: string) => within(cardOf(name)).queryByRole('region', { name: /^重置卡$|^Limit resets$/ });
const row = (name: string, kind: string) => cardOf(name).querySelector(`[data-reset-card="${kind}"]`) as HTMLElement;

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  await i18n.changeLanguage('zh');
});

describe('reset cards in the quota card', () => {
  it('MH-QUOTA-077: draws no block for an account holding none, a vendor without cards, or cards that could not be read', () => {
    draw([
      account('none', 'anthropic', held([])),
      account('vendorless', 'gemini'),
      account('unread', 'openai', { state: 'unread', lock: null, cards: [] }),
    ]);
    for (const name of ['none', 'vendorless', 'unread']) {
      expect(block(name)).toBeNull();
      // The account's own refresh is there whatever it holds.
      expect(within(cardOf(name)).getByRole('button', { name: '刷新这个账号' })).toBeTruthy();
    }
    expect(screen.queryByText(/重置卡/)).toBeNull();
  });

  it('MH-QUOTA-078: lists one row per kind with its count, the vendor\'s own statement or nothing, and the first expiry on its own line', async () => {
    const { unmount } = draw([
      account('claude', 'anthropic', held([
        card({ kind: 'all', expires_at: '2026-12-12T09:00:00Z' }),
        card({ kind: 'fable', count: 3 }),
        card({ kind: 'weekly', expires_at: null }),
      ])),
      // An `all` card whose entry does not name what it clears, and ChatGPT's, which states nothing.
      account('claude-unstated', 'anthropic', held([card({ kind: 'all', stated: false })])),
      account('chatgpt', 'openai', held([card({ kind: 'account', count: 2, stated: false, expires_at: '2027-01-03T09:00:00Z' })])),
    ]);
    const claude = block('claude')!;
    expect(within(claude).getAllByRole('listitem')).toHaveLength(3);
    expect(within(row('claude', 'all')).getByText('全部额度重置卡')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('×1')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('5 小时额度和每周额度回满')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('最早 12月12日 到期')).toBeTruthy();
    expect(within(row('claude', 'fable')).getByText('×3')).toBeTruthy();
    expect(within(row('claude', 'fable')).getByText('退回本周用在 Fable 上的额度，其他模型用掉的不退；5 小时额度回满')).toBeTruthy();
    // No expiry named, no expiry line.
    expect(row('claude', 'weekly').querySelector('[data-reset-card-expiry]')).toBeNull();
    // The expiry has its own line that never wraps.
    expect(row('claude', 'all').querySelector('[data-reset-card-expiry]')!.className).toContain('whitespace-nowrap');

    expect(within(row('claude-unstated', 'all')).queryByText('5 小时额度和每周额度回满')).toBeNull();
    expect(within(row('claude-unstated', 'all')).getByText('全部额度重置卡')).toBeTruthy();
    // ChatGPT's card: name, count and expiry only; a later year is named.
    expect(row('chatgpt', 'account').querySelectorAll('p')).toHaveLength(1);
    expect(within(row('chatgpt', 'account')).getByText('重置卡')).toBeTruthy();
    expect(within(row('chatgpt', 'account')).getByText('最早 2027年1月3日 到期')).toBeTruthy();
    expect(within(row('chatgpt', 'account')).getByRole('button', { name: '使用 重置卡' })).toBeTruthy();
    unmount();

    await i18n.changeLanguage('en');
    draw([account('claude', 'anthropic', held([card({ kind: 'all', expires_at: '2026-12-12T09:00:00Z' }), card({ kind: 'fable', count: 3 })]))]);
    expect(block('claude')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('Full reset')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('The 5-hour and weekly limits go back to full')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('First expires Dec 12')).toBeTruthy();
    expect(within(row('claude', 'fable')).getByRole('button', { name: 'Use Fable reset' })).toBeTruthy();
    expect(within(cardOf('claude')).getByRole('button', { name: 'Refresh this account' })).toBeTruthy();
  });

  it('MH-QUOTA-079: says why a card cannot be used now instead of offering 「使用」', async () => {
    const blocked = [
      card({ kind: 'fable', usable: false, blocked: 'needs_limit' }),
      card({ kind: 'all', usable: false, blocked: 'cooldown', blocked_until: iso(NOW + 130 * MINUTE) }),
      card({ kind: 'session', usable: false, blocked: 'cooldown', blocked_until: null }),
      card({ kind: 'weekly', usable: false, blocked: 'unavailable' }),
    ];
    const { unmount } = draw([account('claude', 'anthropic', held(blocked))]);
    expect(within(block('claude')!).queryByRole('button', { name: /^使用/ })).toBeNull();
    expect(within(row('claude', 'fable')).getByText('到限额后才能用')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('2 小时 10 分后可用')).toBeTruthy();
    expect(within(row('claude', 'session')).getByText('过一会儿才能用')).toBeTruthy();
    expect(within(row('claude', 'weekly')).getByText('供应商现在不让用')).toBeTruthy();
    unmount();

    await i18n.changeLanguage('en');
    draw([account('claude', 'anthropic', held(blocked))]);
    expect(within(row('claude', 'fable')).getByText('Usable once a limit is reached')).toBeTruthy();
    expect(within(row('claude', 'all')).getByText('Usable in 2h 10m')).toBeTruthy();
    expect(within(row('claude', 'session')).getByText('Usable after a short wait')).toBeTruthy();
    expect(within(row('claude', 'weekly')).getByText('Not allowed by the provider now')).toBeTruthy();
  });

  it('MH-QUOTA-080: the confirm step names what it spends and only asks; the other kinds wait', async () => {
    const intents: [string, ResetCardIntent][] = [];
    const onIntent = (sourceId: string, intent: ResetCardIntent) => intents.push([sourceId, intent]);
    const sources = [account('claude', 'anthropic', held([card({ kind: 'all' }), card({ kind: 'fable', count: 3 })]))];
    const { rerender } = draw(sources, { onIntent });
    await userEvent.click(within(row('claude', 'fable')).getByRole('button', { name: '使用 Fable 重置卡' }));
    expect(intents).toEqual([['claude', { type: 'ask', kind: 'fable', stated: true, count: 3 }]]);

    const phases = { claude: { phase: 'confirm', kind: 'fable', stated: true, count: 3 } as ResetCardPhase };
    rerender(
      <I18nextProvider i18n={i18n}>
        <QuotaTab quota={readyRegion({ refresh_interval_seconds: 300, sources })} now={NOW} resetCardPhases={phases} onResetCardIntent={onIntent} canUseResetCards />
      </I18nextProvider>,
    );
    const confirmRow = within(row('claude', 'fable'));
    const note = confirmRow.getByRole('status');
    expect(note.textContent).toBe('用掉 1 张，不能撤销。');
    const confirm = confirmRow.getByRole('button', { name: '确认使用' });
    const cancel = confirmRow.getByRole('button', { name: '取消' });
    // Both buttons are described by the line, so it is heard wherever focus lands.
    expect(confirm.getAttribute('aria-describedby')).toBe(note.id);
    expect(cancel.getAttribute('aria-describedby')).toBe(note.id);
    expect((within(row('claude', 'all')).getByRole('button', { name: '使用 全部额度重置卡' }) as HTMLButtonElement).disabled).toBe(true);

    await userEvent.click(cancel);
    await userEvent.click(confirm);
    expect(intents.slice(1)).toEqual([['claude', { type: 'cancel' }], ['claude', { type: 'confirm', kind: 'fable' }]]);
  });

  it('MH-QUOTA-081: the server\'s lock holds every 「使用」 of its account, says which card is in use or unconfirmed, and leaves other accounts alone', () => {
    const cards = [card({ kind: 'all' }), card({ kind: 'fable', count: 3 })];
    const { rerender } = draw([
      account('claude', 'anthropic', held(cards, { lock: { reason: 'in_flight', kind: 'fable', since: iso(NOW) } })),
      account('chatgpt', 'openai', held([card({ kind: 'account', stated: false })])),
    ]);
    const using = within(row('claude', 'fable')).getByRole('button', { name: '使用中…' }) as HTMLButtonElement;
    expect(using.disabled).toBe(true);
    expect((within(row('claude', 'all')).getByRole('button', { name: '使用 全部额度重置卡' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(cardOf('chatgpt')).getByRole('button', { name: '使用 重置卡' }) as HTMLButtonElement).disabled).toBe(false);

    // The same reading again (a poll) keeps it held; an unconfirmed lock says so in words.
    const unconfirmed = [account('claude', 'anthropic', held(cards, { lock: { reason: 'unconfirmed', kind: 'fable', since: iso(NOW) } }))];
    rerender(
      <I18nextProvider i18n={i18n}>
        <QuotaTab quota={readyRegion({ refresh_interval_seconds: 300, sources: unconfirmed })} now={NOW} canUseResetCards />
      </I18nextProvider>,
    );
    expect(within(row('claude', 'fable')).getByRole('alert').textContent).toBe('没收到供应商的结果，这张卡可能已经用掉。先别再用，过一会儿刷新这个账号看张数。');
    for (const kind of ['all', 'fable']) {
      for (const button of within(row('claude', kind)).getAllByRole('button')) expect((button as HTMLButtonElement).disabled).toBe(true);
    }
 
    // A lock on an account whose cards are not read yet, or whose kind matches no row, is said for the block.
    const sentence = '没收到供应商的结果，这张卡可能已经用掉。先别再用，过一会儿刷新这个账号看张数。';
    rerender(
      <I18nextProvider i18n={i18n}>
        <QuotaTab
          quota={readyRegion({ refresh_interval_seconds: 300, sources: [
            account('unread', 'anthropic', { state: 'unread', lock: { reason: 'unconfirmed', kind: 'all', since: iso(NOW) }, cards: [] }),
            account('other-kind', 'anthropic', held([card({ kind: 'fable' })], { lock: { reason: 'unconfirmed', kind: 'weekly', since: iso(NOW) } })),
          ] })}
          now={NOW}
          canUseResetCards
        />
      </I18nextProvider>,
    );
    expect(within(block('unread')!).getByRole('alert').textContent).toBe(sentence);
    expect(within(block('other-kind')!).getByRole('alert').textContent).toBe(sentence);
    expect((within(block('other-kind')!).getByRole('button', { name: '使用 Fable 重置卡' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('MH-QUOTA-082: says what a use did in the vendor\'s words, or that no card was spent and why, in zh and en', async () => {
    const cards = [card({ kind: 'all', stated: false }), card({ kind: 'fable', count: 2 })];
    const said = (phase: ResetCardPhase) => {
      const { unmount } = draw([account('claude', 'anthropic', held(cards))], { phases: { claude: phase } });
      const line = within(row('claude', phase.kind)).getByRole(phase.phase === 'used' || phase.phase === 'recounted' ? 'status' : 'alert').textContent;
      // Nothing is held by the page's own outcome: the next use is the user's to make.
      const free = !(within(row('claude', 'all')).getByRole('button', { name: /^(使用|Use) / }) as HTMLButtonElement).disabled;
      unmount();
      return [line, free];
    };
    const cases: [ResetCardPhase, string, string, boolean][] = [
      [{ phase: 'used', kind: 'fable', stated: true }, '用了 1 张。本周用在 Fable 上的额度已退回，5 小时额度已回满。', '1 used. The weekly usage spent on Fable is given back; the 5-hour limit is refilled.', true],
      [{ phase: 'used', kind: 'all', stated: false }, '用了 1 张。', '1 used.', true],
      [{ phase: 'not_used', kind: 'fable', reason: 'not_limited' }, '没有用掉：这个账号现在没到限额。', 'Not used: this account isn’t at a limit now.', true],
      [{ phase: 'not_used', kind: 'fable', reason: 'cooldown' }, '没有用掉：刚用过一张，要等一会儿才能再用。', 'Not used: one was used recently, and the next has to wait.', true],
      [{ phase: 'not_used', kind: 'fable', reason: 'refused' }, '没有用掉：供应商拒绝了这次请求。', 'Not used: the provider refused the request.', true],
      [{ phase: 'not_used', kind: 'fable', reason: 'unavailable' }, '没有用掉：供应商现在不让用这张卡。', 'Not used: the provider isn’t allowing this card now.', true],
      [{ phase: 'not_used', kind: 'fable', reason: 'held' }, '没有用掉：这个账号现在不能用这种卡。', 'Not used: this account can’t use this kind of card right now.', true],
      [{ phase: 'not_used', kind: 'fable', reason: null }, '没有用掉。', 'Not used.', true],
      // The count changed before anything was sent: said plainly, as a note, never as a failure.
      [{ phase: 'recounted', kind: 'fable' }, '张数变了，已刷新。', 'The count changed; refreshed.', true],
      // No answer came back: held until the account is read again.
      [{ phase: 'unconfirmed', kind: 'fable' }, '没收到供应商的结果，这张卡可能已经用掉。先别再用，过一会儿刷新这个账号看张数。', 'The provider’s answer didn’t arrive, so this card may be spent. Don’t use another yet; refresh this account in a moment and check the count.', false],
    ];
    for (const [phase, zhLine, , free] of cases) expect(said(phase)).toEqual([zhLine, free]);
    await i18n.changeLanguage('en');
    for (const [phase, , enLine, free] of cases) expect(said(phase)).toEqual([enLine, free]);
  });

  it('MH-QUOTA-088: a reader who may not spend cards sees the same list with no use action, whatever the server holds', () => {
    const cards = [
      card({ kind: 'all' }),
      card({ kind: 'fable', count: 3 }),
      card({ kind: 'weekly', usable: false, blocked: 'needs_limit' }),
    ];
    const lock = { reason: 'unconfirmed', kind: 'fable', since: iso(NOW) } as const;
    draw([
      account('claude', 'anthropic', held(cards)),
      account('locked', 'anthropic', held(cards, { lock })),
    ], { canUse: false, phases: { claude: { phase: 'confirm', kind: 'fable', stated: true, count: 3 } } });
    for (const name of ['claude', 'locked']) {
      const section = block(name)!;
      expect(within(section).getAllByRole('listitem')).toHaveLength(3);
      expect(within(section).getByText('×3')).toBeTruthy();
      expect(within(section).getByText('5 小时额度和每周额度回满')).toBeTruthy();
      expect(within(section).getAllByText('最早 11月28日 到期')).toHaveLength(3);
      // No 「使用」, no disabled button, no confirm line, no lock line.
      expect(within(section).queryAllByRole('button')).toHaveLength(0);
      expect(within(section).queryByRole('status')).toBeNull();
      expect(within(section).queryByRole('alert')).toBeNull();
      expect(within(section).getByText('到限额后才能用')).toBeTruthy();
      // The account's own refresh stays for everyone.
      expect((within(cardOf(name)).getByRole('button', { name: '刷新这个账号' }) as HTMLButtonElement).disabled).toBe(false);
    }
  });

  it('MH-QUOTA-083: still says what happened when the card used was the last of its kind', () => {
    draw([account('claude', 'anthropic', held([]))], { phases: { claude: { phase: 'used', kind: 'session', stated: true } } });
    const section = block('claude')!;
    expect(within(section).queryAllByRole('listitem')).toHaveLength(0);
    expect(within(section).getByRole('status').textContent).toBe('用了 1 张。5 小时额度已回满。');
  });
});

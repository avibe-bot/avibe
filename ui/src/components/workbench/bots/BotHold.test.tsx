/* @vitest-environment jsdom */

import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { BotsContext, type BotsState } from '../../../context/BotsContext';
import type { Bot } from '../../../lib/bots';
import { BotHoldProvider } from './BotHold';
import { useBotHold } from './BotHoldContext';

// The hold's own path is useHoldToTalk's test, and the gesture on the strip is the browser's
// (e2e/voice-shortcut/strip.spec.ts). Here: the one hold every avatar shares stays usable when the
// Bot a take was for goes away.
const hold = vi.hoisted(() => ({
  phase: 'idle' as string,
  discard: vi.fn(),
  options: null as null | { context?: () => unknown },
  listSessionMessages: vi.fn(),
}));
vi.mock('../../voice/useHoldToTalk', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../voice/useHoldToTalk')>()),
  useHoldToTalk: (options: { context?: () => unknown }) => ({
    ...((hold.options = options), {}),
    phase: hold.phase, interruptReason: 'send-failed', text: 'heard', stash: '', seconds: 3, progress: 0, outcome: null,
    available: true, holdable: hold.phase !== 'interrupted', canRetry: false,
    bind: {
      onPointerDown: () => {}, onPointerMove: () => {}, onPointerUp: () => {}, onPointerCancel: () => {},
      onLostPointerCapture: () => {}, onClick: () => {}, onContextMenu: () => {},
    },
    sendInterrupted: () => {}, retry: () => {}, discard: hold.discard, dismiss: () => {},
  }),
}));
vi.mock('../../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../context/ApiContext')>()),
  useApi: () => ({ sendSessionMessage: vi.fn(), listSessionMessages: hold.listSessionMessages }),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const bot = (handle: string): Bot => ({
  handle, display_name: handle, avatar: null, role: '', agent_name: handle, session_id: `ses_${handle}`, builtin: false, hide_details: true,
});
const pm = bot('pm');
const cso = bot('cso');

const state = (bots: Bot[]): BotsState => ({
  enabled: true,
  bots,
  loaded: true,
  listed: true,
  mainBot: null,
  limit: { count: bots.length, max: 5 },
  atLimit: false,
  statusBySession: {},
  botForSession: (id) => bots.find((each) => each.session_id === id),
  botForAgent: (name) => bots.find((each) => each.agent_name === name),
  botForHandle: (handle) => bots.find((each) => each.handle === handle),
  displayName: (each) => each.display_name,
  afterWrite: async () => undefined,
  holdOrder: () => () => undefined,
  setHideDetails: async () => true,
  openProfile: () => undefined,
  profileRequest: null,
  closeProfile: () => undefined,
});

/** An avatar bound to the shared hold, as the strip binds each of its own. */
function Avatar({ of }: { of: Bot }) {
  const { bind } = useBotHold();
  const handlers = bind?.(of);
  return <button type="button" onPointerDown={(event) => handlers?.onPointerDown(event, () => {})}>{of.handle}</button>;
}

const view = (bots: Bot[]) => (
  <MemoryRouter>
    <BotsContext.Provider value={state(bots)}>
      <BotHoldProvider>
        <Avatar of={pm} />
      </BotHoldProvider>
    </BotsContext.Provider>
  </MemoryRouter>
);

describe('the hold every Bot avatar shares', () => {
  afterEach(() => {
    cleanup();
    hold.phase = 'idle';
    hold.discard.mockReset();
    hold.listSessionMessages.mockReset();
  });

  it("hears a take in the held Bot's own conversation", async () => {
    hold.listSessionMessages.mockResolvedValue({
      messages: [
        { author: 'user', type: 'user', text: '下周的发布排期呢', content: {} },
        { author: 'agent', type: 'result', text: '周四发 v2.3', content: {} },
      ],
      next_after_id: null,
    });
    const { getByText } = render(view([pm, cso]));
    fireEvent.pointerDown(getByText('pm'), { button: 0, pointerType: 'mouse' });

    await expect(hold.options?.context?.()).resolves.toEqual({ reply: '周四发 v2.3', userPrevious: '下周的发布排期呢' });
    expect(hold.listSessionMessages).toHaveBeenCalledWith('ses_pm', expect.objectContaining({ tail: true, cache: false }));
  });

  it("discards a take whose Bot is removed while it waits, so the other avatars can hold again", () => {
    const { getByText, rerender } = render(view([pm, cso]));
    fireEvent.pointerDown(getByText('pm'), { button: 0, pointerType: 'mouse' });
    // The send failed: the take waits on its card for the reader.
    hold.phase = 'interrupted';
    rerender(view([pm, cso]));
    expect(hold.discard).not.toHaveBeenCalled();

    // The PM is removed from Bots (its menu is still there): the take has nowhere to go.
    rerender(view([cso]));
    expect(hold.discard).toHaveBeenCalledTimes(1);
  });
});

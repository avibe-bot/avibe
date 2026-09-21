// @vitest-environment jsdom
//
// C5: one frame, three methods. What that rule protects is a comparison — someone
// switching between 「订阅」 and 「API Key」 to decide which they have — so every case
// here is a way the switch could cost them something: a frame that rebuilt itself,
// a draft that was thrown away, a control still reachable in a pane nobody is
// looking at, or a method that vanished and took the dialog's contents with it.
//
// The geometry half of C5 (the box not resizing) is browser evidence and lives in
// `providers.spec.ts`. What is provable here is the structural half it rests on:
// the anchored parts are the same nodes before and after, and only the body
// changes.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { apiKeyVendorPreset } from '@/components/settings/models/apiKeyVendors';
import { createSourceCollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import type { Source } from '@/components/settings/models/types';

const showToast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

// The authorization flow owns its own frame, poll and cancellation; what this file
// has to prove is how its two callback shapes are read. Standing in for it keeps
// that readable without driving a device-code flow to get there.
type OAuthProps = {
  vendor: string | null;
  onConnected: (source?: Source, placement?: unknown) => void;
  onClose: () => void;
};
const oauth = vi.hoisted(() => ({ current: null as OAuthProps | null }));
vi.mock('@/components/settings/models/OAuthConnectDialog', () => ({
  OAuthConnectDialog: (props: OAuthProps) => {
    oauth.current = props;
    return React.createElement('div', { 'data-testid': 'oauth-stub' });
  },
}));

import { ApiCallError, modelsApi, type SourceCreated } from '@/components/settings/models/modelsApi';
import { AddSourceDialog } from './AddSourceDialog';
import type { ProviderSlot } from './providerStage';

const slot = (vendor: string, over: Partial<ProviderSlot> = {}): ProviderSlot => ({
  vendor,
  brand: vendor,
  label: vendor === 'openai' ? 'OpenAI' : vendor === 'anthropic' ? 'Anthropic' : vendor,
  kind: 'detected',
  mask: 'sk-…9f21',
  supply: null,
  pending: false,
  // Takeable from here, and no reason not to be: the blocked shape is the
  // opposite pair — no backend may take it, and it says why.
  backends: ['codex'],
  reasons: [],
  ...over,
});

/** Found, and not this screen's to take: the rows the stage keeps visible anyway. */
const blockedSlot = (vendor: string, over: Partial<ProviderSlot> = {}): ProviderSlot =>
  slot(vendor, { backends: [], reasons: ['onboarding.import.outOfScope'], ...over });

const source = (over: Partial<Source> & { id: string; vendor: string }): Source => ({
  last_discovered_at: null,
  kind: 'api_key',
  display_name: over.vendor,
  protocol: 'openai_chat',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active' },
  models: [],
  ...over,
});

const created = (row: Source): SourceCreated => ({ source: row, added_to: [], adopted_by: [] });

const onToggleDetected = vi.fn();
const onReviewDetected = vi.fn();
const onAdded = vi.fn<(value: SourceCreated | null) => Promise<void>>();
const onClose = vi.fn();

type Options = {
  more?: boolean;
  vendor?: string | null;
  detected?: ProviderSlot[];
  pendingCount?: number;
  sources?: Source[];
  selected?: (slot: ProviderSlot) => boolean;
  writable?: boolean;
};

const renderDialog = ({
  more = true,
  vendor = null,
  detected = [],
  pendingCount = 0,
  sources = [],
  selected = () => false,
  writable = true,
}: Options = {}) => {
  const view = render(
    <I18nextProvider i18n={i18n}>
      <AddSourceDialog
        more={more}
        vendor={vendor}
        detected={detected}
        pendingCount={pendingCount}
        writable={writable}
        sources={sources}
        sourceReads={createSourceCollectionReadAuthority()}
        isSelected={selected}
        onToggleDetected={onToggleDetected}
        onReviewDetected={onReviewDetected}
        onAdded={onAdded}
        onClose={onClose}
      />
    </I18nextProvider>,
  );
  const rerender = (next: Options) => view.rerender(
    <I18nextProvider i18n={i18n}>
      <AddSourceDialog
        more={next.more ?? more}
        vendor={next.vendor ?? vendor}
        detected={next.detected ?? detected}
        pendingCount={next.pendingCount ?? pendingCount}
        writable={next.writable ?? writable}
        sources={next.sources ?? sources}
        sourceReads={createSourceCollectionReadAuthority()}
        isSelected={next.selected ?? selected}
        onToggleDetected={onToggleDetected}
        onReviewDetected={onReviewDetected}
        onAdded={onAdded}
        onClose={onClose}
      />
    </I18nextProvider>,
  );
  return { ...view, rerender };
};

const frame = () => {
  const dialog = document.querySelector<HTMLElement>('.setup-add-dialog');
  if (!dialog) throw new Error('no dialog');
  return dialog;
};
const body = () => {
  const pane = document.querySelector<HTMLElement>('.setup-add-body');
  if (!pane) throw new Error('no body');
  return pane;
};
const methodButtons = () => [...document.querySelectorAll<HTMLElement>('.setup-add-method')];
const methodNames = () => methodButtons().map((button) => button.textContent);
const activeMethod = () => body().dataset.method;
const chooseMethod = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
  await user.click(screen.getByRole('button', { name }));
};
/** The footer's two buttons, in the order they are read: cancel, then the offer. */
const footButtons = () => [...document.querySelectorAll<HTMLButtonElement>('.setup-add-foot button')];
const cancelButton = () => footButtons()[0];
const primary = () => footButtons()[footButtons().length - 1];

beforeEach(async () => {
  onToggleDetected.mockReset();
  onReviewDetected.mockReset();
  onClose.mockReset();
  onAdded.mockReset();
  onAdded.mockResolvedValue(undefined);
  oauth.current = null;
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  // Radix measures the viewport for the popover the vendor picker opens into.
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  showToast.mockReset();
});

describe('AddSourceDialog — the frame', () => {
  it('offers Detected only when something was detected', async () => {
    renderDialog();
    expect(methodNames()).toEqual(['Subscription', 'API Key']);
    cleanup();

    renderDialog({ detected: [slot('openai')] });
    expect(methodNames()).toEqual(['Detected', 'Subscription', 'API Key']);
    // And it opens on it: what is already on the machine is the cheapest way in.
    expect(activeMethod()).toBe('detected');
  });

  it('keeps one frame across repeated switching, changing only the body', async () => {
    renderDialog({ detected: [slot('openai')] });
    const user = userEvent.setup();
    const dialog = frame();
    const head = document.querySelector('.setup-add-head');
    const foot = document.querySelector('.setup-add-foot');
    const buttons = methodButtons();
    const pane = body();

    for (const round of ['API Key', 'Subscription', 'Detected', 'API Key', 'Detected']) {
      await chooseMethod(user, round);
      // The same elements, not merely matching ones: a frame React rebuilt would
      // lose scroll position, focus and any transition mid-flight.
      expect(frame()).toBe(dialog);
      expect(document.querySelector('.setup-add-head')).toBe(head);
      expect(document.querySelector('.setup-add-foot')).toBe(foot);
      expect(methodButtons()).toEqual(buttons);
      expect(body()).toBe(pane);
      // One pane at a time, named by the body itself so the stylesheet sizes the
      // frame by the method rather than by its contents.
      expect(activeMethod()).toBe(
        round === 'API Key' ? 'apiKey' : round === 'Subscription' ? 'subscription' : 'detected',
      );
    }
  });

  it('states the chosen method exactly once', async () => {
    renderDialog({ detected: [slot('openai')] });
    const user = userEvent.setup();
    await chooseMethod(user, 'Subscription');

    const pressed = methodButtons().filter((button) => button.getAttribute('aria-pressed') === 'true');
    expect(pressed.map((button) => button.textContent)).toEqual(['Subscription']);
    expect(document.querySelector('[role="group"][aria-label="Add method"]')).toBeTruthy();
  });

  it('leaves nothing of an unchosen method behind', async () => {
    renderDialog({ detected: [slot('openai')] });
    const user = userEvent.setup();

    await chooseMethod(user, 'API Key');
    // The subscription rows are gone, not hidden: a hidden pane keeps its effects,
    // and this one's is an authorization flow where a duplicate is a second device
    // code for the same account.
    expect(within(body()).queryByRole('button', { name: /Sign in with/ })).toBeNull();
    expect(within(body()).queryByRole('button', { name: /Select existing/ })).toBeNull();
    expect(within(body()).getByLabelText('API key')).toBeTruthy();

    await chooseMethod(user, 'Subscription');
    expect(within(body()).queryByLabelText('API key')).toBeNull();
    expect(within(body()).queryByLabelText('Base URL')).toBeNull();
    // Nothing reachable by keyboard either, which is the part a `hidden` attribute
    // would have covered and the part a person actually trips over.
    expect(within(body()).getAllByRole('button').every((node) => node.closest('.setup-add-body'))).toBe(true);
  });

  it('falls back when the method someone is on stops existing', async () => {
    const { rerender } = renderDialog({ detected: [slot('openai')] });
    expect(activeMethod()).toBe('detected');

    // The last detected candidate was taken over while this was open.
    rerender({ detected: [] });

    expect(methodNames()).toEqual(['Subscription', 'API Key']);
    // A frame rendering nothing at all would be the alternative.
    expect(activeMethod()).toBe('subscription');
  });

  it('says what it is for, without changing what it can do', async () => {
    const { rerender } = renderDialog({ more: true });
    expect(screen.getByText('Add more model providers')).toBeTruthy();
    const methods = methodNames();

    rerender({ more: false });

    expect(screen.getByText('Add subscription or API Key')).toBeTruthy();
    expect(methodNames()).toEqual(methods);
  });
});

describe('AddSourceDialog — what a switch preserves', () => {
  it('keeps a half-typed key across a switch away and back', async () => {
    renderDialog();
    const user = userEvent.setup();
    await chooseMethod(user, 'API Key');
    await user.type(within(body()).getByLabelText('Base URL'), 'https://proxy.example/v1');
    await user.type(within(body()).getByLabelText('API key'), 'sk-half-typed');

    await chooseMethod(user, 'Subscription');
    await chooseMethod(user, 'API Key');

    expect((within(body()).getByLabelText('Base URL') as HTMLInputElement).value)
      .toBe('https://proxy.example/v1');
    expect((within(body()).getByLabelText('API key') as HTMLInputElement).value).toBe('sk-half-typed');
    // And it is still submittable: the draft came back whole, not just its text.
    expect(primary().disabled).toBe(false);
  });

  it('keeps the subscription someone had chosen', async () => {
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Sign in with Claude/ }));
    expect(primary().textContent).toContain('Claude');

    await chooseMethod(user, 'API Key');
    await chooseMethod(user, 'Subscription');

    expect(primary().textContent).toContain('Claude');
  });

  it('opens a named brand on the way in that brand is offered', async () => {
    renderDialog({ more: false, vendor: 'anthropic' });
    // A subscription brand opens on its sign-in rather than on a key field.
    expect(activeMethod()).toBe('subscription');
    expect(primary().textContent).toContain('Claude');
    cleanup();

    renderDialog({ more: false, vendor: 'deepseek' });
    expect(activeMethod()).toBe('apiKey');
    // With the endpoint that brand publishes already filled: re-selecting the
    // vendor that is already selected is identity, so a prefill left undone here
    // could not be reached from inside the form at all.
    expect((within(body()).getByLabelText('Base URL') as HTMLInputElement).value)
      .toBe(apiKeyVendorPreset('deepseek')?.official_base_url);
    expect(within(body()).getByLabelText('DeepSeek API Key')).toBeTruthy();
  });
});

describe('AddSourceDialog — detected', () => {
  it('counts the selection in the footer and hands it to the takeover', async () => {
    renderDialog({ detected: [slot('openai')], pendingCount: 0 });
    expect(primary().disabled).toBe(true);
    cleanup();

    renderDialog({ detected: [slot('openai'), slot('gemini')], pendingCount: 2 });
    expect(primary().textContent).toBe('Review 2 selected');
    await userEvent.setup().click(primary());

    // Consent belongs to the shipped takeover; this dialog only points at it.
    expect(onReviewDetected).toHaveBeenCalledTimes(1);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it('marks what is already there and offers no choice about it', async () => {
    renderDialog({
      detected: [slot('openai'), slot('gemini')],
      sources: [source({ id: 'src_openai', vendor: 'openai' })],
    });
    const user = userEvent.setup();
    const [added, open] = [...document.querySelectorAll<HTMLButtonElement>('.setup-add-row')];

    expect(added.dataset.state).toBe('added');
    expect(within(added).getByText('Added')).toBeTruthy();
    expect(added.disabled).toBe(true);
    expect(added.getAttribute('aria-pressed')).toBeNull();

    await user.click(open);
    expect(onToggleDetected).toHaveBeenCalledTimes(1);
    expect(onToggleDetected.mock.calls[0][0]).toMatchObject({ vendor: 'gemini' });
  });

  it('keeps a credential nobody may take here, and says why instead of offering it', async () => {
    renderDialog({ detected: [blockedSlot('anthropic'), slot('openai')], selected: () => true });
    const user = userEvent.setup();
    const [blocked, open] = [...document.querySelectorAll<HTMLButtonElement>('.setup-add-row')];

    // Still on the list, with the migration feature's own sentence about it. The
    // alternative — dropping it — is how a person ends up adding a second copy of a
    // key that is already on the machine.
    expect(blocked.dataset.blocked).toBe('true');
    expect(within(blocked).getByText(/can be migrated, but not from here/)).toBeTruthy();
    expect(blocked.disabled).toBe(true);
    // Not a toggle that happens to be off: there is no consent to give here, and
    // `aria-pressed="false"` beside a selected sibling would say there is.
    expect(blocked.getAttribute('aria-pressed')).toBeNull();
    expect(open.getAttribute('aria-pressed')).toBe('true');

    await user.click(blocked);
    expect(onToggleDetected).not.toHaveBeenCalled();
  });

  it('opens on something that can be done when every detection is blocked', async () => {
    renderDialog({ detected: [blockedSlot('anthropic')] });
    const user = userEvent.setup();

    // The method still exists — it is where the explanation lives — but landing on
    // a pane whose every row is disabled answers nothing for someone who came here
    // to add a provider.
    expect(methodNames()).toEqual(['Detected', 'Subscription', 'API Key']);
    expect(activeMethod()).toBe('subscription');

    await chooseMethod(user, 'Detected');
    expect(document.querySelectorAll('.setup-add-row')).toHaveLength(1);
    // Nothing to hand to the take-over, so nothing is offered.
    expect(primary().disabled).toBe(true);
    await user.click(primary());
    expect(onReviewDetected).not.toHaveBeenCalled();
  });
});

describe('AddSourceDialog — the one write it owns', () => {
  const fill = async (user: ReturnType<typeof userEvent.setup>) => {
    await chooseMethod(user, 'API Key');
    await user.type(within(body()).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(body()).getByLabelText('API key'), 'sk-live-1');
  };

  it('saves on the person\'s word, reads back, and closes once the caller has', async () => {
    const row = source({ id: 'src_new', vendor: 'custom', client_nonce: 'scn_x' });
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue(created(row));
    let release!: () => void;
    onAdded.mockImplementation(() => new Promise<void>((resolve) => { release = () => resolve(); }));
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());

    await waitFor(() => expect(onAdded).toHaveBeenCalledWith(created(row)));
    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0][0]).toMatchObject({
      kind: 'api_key',
      base_url: 'https://api.example/v1',
      key: 'sk-live-1',
      // Deliberate policy, the same in both hosts of this form: a provider that is
      // briefly down does not cost someone the key they just pasted.
      save_unverified: true,
    });
    expect(create.mock.calls[0][0].client_nonce).toMatch(/^scn_/);

    // Still open while the caller reads back, and locked shut while it does.
    expect(screen.getByText('Checking the model connection…')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    // Every way out, including the one in the corner: a write in flight is not a
    // state anyone should be able to abandon the frame on.
    expect(cancelButton().disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Close' }) as HTMLButtonElement).disabled).toBe(true);
    expect(methodButtons().every((button) => (button as HTMLButtonElement).disabled)).toBe(true);

    release();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('keeps the entries when the server refuses, and sends again on retry', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new ApiCallError('source_invalid', 'bad key', true, [], [], [], 400))
      .mockResolvedValueOnce(created(source({ id: 'src_new', vendor: 'custom' })));
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());

    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());
    expect((within(body()).getByLabelText('API key') as HTMLInputElement).value).toBe('sk-live-1');
    expect(primary().textContent).toBe('Retry');
    expect(onClose).not.toHaveBeenCalled();

    await user.click(primary());

    // A server verdict is a verdict: the same request may simply be sent again.
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('adopts a write whose outcome was unknown instead of sending a second one', async () => {
    const landed = source({ id: 'src_landed', vendor: 'custom' });
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());

    // It had landed after all — the response was what was lost.
    const nonce = create.mock.calls[0][0].client_nonce;
    list.mockResolvedValue([{ ...landed, client_nonce: nonce }]);
    await user.click(primary());

    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onAdded.mock.calls[0][0]).toMatchObject({ source: { id: 'src_landed' } });
    // Repeating the write is exactly how a second identical source appears.
    expect(create).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('sends the write the readback proved absent', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504))
      .mockResolvedValueOnce(created(source({ id: 'src_new', vendor: 'custom' })));
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());
    await user.click(primary());

    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('leaves an unreadable inventory as unknown as it was', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    vi.spyOn(modelsApi, 'listSources').mockRejectedValue(new Error('offline'));
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());
    await user.click(primary());

    // Neither adopted nor re-sent: an unknown outcome that could not be read is
    // still unknown, and the retry is still there to take.
    await waitFor(() => expect(primary().textContent).toBe('Retry'));
    expect(create).toHaveBeenCalledTimes(1);
    expect(onAdded).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('hands an unknown outcome back on the way out rather than forgetting it', async () => {
    const landed = source({ id: 'src_landed', vendor: 'custom' });
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());

    // It had landed. The nonce that would identify it lives in this frame, so
    // leaving without reading would make it unattributable: the parent would show
    // nothing, and the next attempt would carry a new nonce and write a duplicate.
    const nonce = create.mock.calls[0][0].client_nonce;
    list.mockResolvedValue([{ ...landed, client_nonce: nonce }]);

    await user.click(cancelButton());

    // Closed on the press, not held until the read answers — nobody is kept in a
    // dialog because a request timed out.
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onAdded.mock.calls[0][0]).toMatchObject({ source: { id: 'src_landed' } });
    // And still exactly one write: closing is not a retry.
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('closes on an unknown outcome it could not read, and says only that', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    vi.spyOn(modelsApi, 'listSources').mockRejectedValue(new Error('offline'));
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());

    await user.click(cancelButton());

    expect(onClose).toHaveBeenCalledTimes(1);
    // A read that failed leaves the outcome exactly as unknown as it already was.
    // Reporting nothing named is what makes the parent refresh and say so.
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith(null));
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('cancels without writing anything', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource');
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(cancelButton());

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(create).not.toHaveBeenCalled();
  });

  it('names its two ways out differently', async () => {
    renderDialog();

    // Both close the frame, and a screen reader that hears 「取消」 twice cannot
    // tell which one it is on.
    expect(cancelButton().textContent).toBe('Cancel');
    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy();
  });

  it('refuses from the keyboard exactly what the footer refuses', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockResolvedValue(created(source({ id: 'src_new', vendor: 'custom' })));
    renderDialog();
    const user = userEvent.setup();
    await chooseMethod(user, 'API Key');
    await user.type(within(body()).getByLabelText('Base URL'), 'https://api.example/v1');

    await user.type(within(body()).getByLabelText('API key'), '{Enter}');

    // Enter IS the footer button. An incomplete draft sent from the keyboard would
    // be the same write the disabled control is there to refuse.
    expect(primary().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();

    await user.type(within(body()).getByLabelText('API key'), 'sk-live-1{Enter}');

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  });

  it('refuses every method\'s offer once the screen stops admitting a write', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockResolvedValue(created(source({ id: 'src_new', vendor: 'custom' })));
    const { rerender } = renderDialog({ detected: [slot('openai')], pendingCount: 1 });
    const user = userEvent.setup();
    await chooseMethod(user, 'API Key');
    await user.type(within(body()).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(body()).getByLabelText('API key'), 'sk-live-1');
    expect(primary().disabled).toBe(false);

    // The engine this write would go to stopped being one the screen can vouch for,
    // while the form was being filled. The frame stays — what is in it is the
    // person's, and the read may come back — and the offer is what is withdrawn.
    rerender({ writable: false });
    expect(frame()).toBeTruthy();
    expect(primary().disabled).toBe(true);
    await user.type(within(body()).getByLabelText('API key'), '{Enter}');
    expect(create).not.toHaveBeenCalled();

    // Every method's offer is a write: a key saved, a sign-in started, a batch handed
    // to the take-over.
    await chooseMethod(user, 'Subscription');
    expect(primary().disabled).toBe(true);
    await chooseMethod(user, 'Detected');
    expect(primary().disabled).toBe(true);
    expect(onReviewDetected).not.toHaveBeenCalled();
  });

  it('does not resend a write whose permission was withdrawn while it read back', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    let answer!: (rows: Source[]) => void;
    const list = vi.spyOn(modelsApi, 'listSources')
      .mockImplementation(() => new Promise<Source[]>((resolve) => { answer = resolve; }));
    const { rerender } = renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());

    // The retry reads the inventory back before deciding whether to send: an unsettled
    // failure may have landed, and a blind second write is how a duplicate appears.
    await user.click(primary());
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));

    // Permission goes away INSIDE that read. The press that started it saw a screen
    // that admitted a write; by the time the readback answers, that is a memory of a
    // permission rather than one, and the POST is where the current answer counts.
    rerender({ writable: false });
    await act(async () => { answer([]); });

    expect(create).toHaveBeenCalledTimes(1);
    // And nothing was thrown away in the refusing: the entries, the retry and the
    // identity that write was sent under are all still here, so the outcome nobody
    // knows yet is still reconcilable rather than abandoned.
    expect((within(body()).getByLabelText('API key') as HTMLInputElement).value).toBe('sk-live-1');
    expect(primary().textContent).toBe('Retry');
    expect(onAdded).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    const nonce = create.mock.calls[0][0].client_nonce;
    list.mockResolvedValue([{ ...source({ id: 'src_landed', vendor: 'custom' }), client_nonce: nonce }]);
    rerender({ writable: true });
    await user.click(primary());
    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onAdded.mock.calls[0][0]).toMatchObject({ source: { id: 'src_landed' } });
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('delivers the source a readback found even once no further write is admitted', async () => {
    const landed = source({ id: 'src_landed', vendor: 'custom' });
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValue(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504));
    let answer!: (rows: Source[]) => void;
    vi.spyOn(modelsApi, 'listSources')
      .mockImplementation(() => new Promise<Source[]>((resolve) => { answer = resolve; }));
    const { rerender } = renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());
    await user.click(primary());
    await waitFor(() => expect(modelsApi.listSources).toHaveBeenCalledTimes(1));

    // Same withdrawal, and this time the read says the write had landed. A source that
    // exists is a receipt: permission governs the next write, never the delivery of one
    // that already happened. Withholding it would hide a credential the person owns.
    rerender({ writable: false });
    const nonce = create.mock.calls[0][0].client_nonce;
    await act(async () => { answer([{ ...landed, client_nonce: nonce }]); });

    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onAdded.mock.calls[0][0]).toMatchObject({ source: { id: 'src_landed' } });
    expect(create).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('locks the draft an unknown outcome is still attached to', async () => {
    vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new ApiCallError('gateway_timeout', 'timeout', true, [], [], [], 504))
      .mockRejectedValueOnce(new ApiCallError('source_invalid', 'bad key', true, [], [], [], 400));
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    renderDialog();
    const user = userEvent.setup();
    await fill(user);

    await user.click(primary());
    await waitFor(() => expect(screen.getByText(/Your entries are preserved/)).toBeTruthy());

    // Reconciliation adopts the row the ORIGINAL draft wrote, so an edit made under
    // an unknown outcome is one the retry can discard without saying so.
    expect((within(body()).getByLabelText('API key') as HTMLInputElement).disabled).toBe(true);
    expect((within(body()).getByLabelText('Base URL') as HTMLInputElement).disabled).toBe(true);

    await user.click(primary());

    // A verdict ends that: the server answered about this draft, so editing it is
    // the whole point of keeping the entries.
    await waitFor(() => expect((within(body()).getByLabelText('API key') as HTMLInputElement).disabled).toBe(false));
    expect((within(body()).getByLabelText('Base URL') as HTMLInputElement).disabled).toBe(false);
  });
});

describe('AddSourceDialog — what comes back from an authorization', () => {
  const signIn = async () => {
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Sign in with Claude/ }));
    await user.click(primary());
    await screen.findByTestId('oauth-stub');
    return user;
  };

  it('reads an argument-less arrival as stale rows, not as a provider added', async () => {
    await signIn();

    // The shipped dialog fires this on every terminal arrival — failures,
    // cancellations, cleanup, a flow resolved after its frame closed.
    act(() => oauth.current?.onConnected());

    await waitFor(() => expect(onAdded).toHaveBeenCalledWith(null));
    // Closing here would report someone's cancelled sign-in as a provider they
    // added, and take the frame they were working in with it.
    expect(onClose).not.toHaveBeenCalled();
    expect(frame()).toBeTruthy();
    expect(screen.getByTestId('oauth-stub')).toBeTruthy();
  });

  it('lands a source that really arrived, with the placement it arrived in', async () => {
    const row = source({ id: 'src_claude', vendor: 'anthropic' });
    await signIn();

    act(() => oauth.current?.onConnected(row, { added_to: ['claude'], adopted_by: ['claude'] }));

    await waitFor(() => expect(onAdded).toHaveBeenCalledWith({
      source: row,
      added_to: ['claude'],
      adopted_by: ['claude'],
    }));
    // The read happens BEHIND the flow's own success panel, which owns the report
    // of where the source landed and the handoff that dismisses it. Replacing that
    // with this dialog's spinner would discard the one thing worth reading.
    expect(screen.getByTestId('oauth-stub')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();

    act(() => oauth.current?.onClose());

    // And when the flow does hand back, there is nothing left to add here.
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('returns to the frame it was launched from when nothing arrived', async () => {
    const user = await signIn();

    act(() => oauth.current?.onClose());

    // A cancelled sign-in is not an ending: the method row, the draft and the
    // chosen brand are all still what they were.
    await waitFor(() => expect(screen.queryByTestId('oauth-stub')).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    expect(activeMethod()).toBe('subscription');
    expect(primary().textContent).toContain('Claude');

    await chooseMethod(user, 'API Key');
    expect(within(body()).getByLabelText('API key')).toBeTruthy();
  });
});

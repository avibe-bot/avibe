import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { WorkbenchSession } from '../../context/ApiContext';
import { SessionRow } from './WorkbenchSidebar';
import { SESSION_ROW_MENU_POSITION_CLASS, SESSION_ROW_PIN_POSITION_CLASS } from './sessionRowLayout';

// The row reaches five providers (four of them through useSessionActions). Replacing
// just those hooks — and keeping the rest of each module real — renders the row itself
// without mounting the whole Workbench tree around it. Mocked rather than provider-
// wrapped so the rendered markup is the ROW and nothing else: the real ToastProvider
// contributes its own absolutely-positioned container, which the rail assertion below
// would read as a third rail control.
vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ApiContext')>()),
  useApi: () => ({ setSessionVisibility: vi.fn() }),
}));
vi.mock('../../context/WorkbenchProjectsContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/WorkbenchProjectsContext')>()),
  useWorkbenchProjectsActions: () => ({
    renameSession: vi.fn(),
    forkSession: vi.fn(),
    setSessionPinned: vi.fn(),
    archiveSession: vi.fn(),
  }),
}));
vi.mock('../../context/ToastContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ToastContext')>()),
  useToast: () => ({ showToast: vi.fn() }),
}));
vi.mock('../../context/ComposerBridgeContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ComposerBridgeContext')>()),
  useComposerInsertTarget: () => null,
}));
vi.mock('../../context/useUnsavedChangesActionGuard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/useUnsavedChangesActionGuard')>()),
  useUnsavedChangesActionGuard: () => () => null,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const render = (ui: ReactElement) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{ui}</MemoryRouter>
    </I18nextProvider>,
  );

const session = (over: Partial<WorkbenchSession> = {}): WorkbenchSession =>
  ({
    id: 'ses_01J8XK5M8T',
    scope_id: 'scope-1',
    project_id: 'proj-1',
    title: 'Model Hub',
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    native_session_id: 'native-1',
    ...over,
  }) as WorkbenchSession;

const row = (over: { canManageMetadata: boolean; pinned?: boolean }) =>
  render(
    <SessionRow
      projectId="proj-1"
      session={session({ pinned: over.pinned ?? false })}
      unread={0}
      canChat
      canManageMetadata={over.canManageMetadata}
    />,
  );

const countTriggers = (markup: string) =>
  markup.split(`aria-label="${en.workbench.sessionActions}"`).length - 1;

// Every control on the row's right-hand rail is placed by a shared offset constant,
// so the rendered offsets are the geometry contract: a control added at a literal
// offset shows up here as an extra (or wrong) entry.
const railOffsets = (markup: string) => (markup.match(/\bright-[^\s"]+/g) ?? []).sort();

// ── The ⋯ trigger and the menu it opens are one control ──────────────────────
// A merge resolution re-introduced the pre-#1194 trigger next to the refactored one,
// so the row printed TWO ⋯ buttons: the reinstated copy sat at a literal `right-2`,
// i.e. half-overlapping the pin's hit area. The surviving copy was also left OUTSIDE
// the `canManageMetadata` gate while the Popover it triggers is gated on it, so a
// read-only row rendered a button that could not open anything.
//
// The property, not the two symptoms: the row offers an actions trigger exactly when
// its Popover can open — `render count === Number(canManageMetadata)` over the whole
// domain of the flag, so neither a duplicate nor a dead trigger can return.
describe('session row actions rail', () => {
  it('renders one ⋯ trigger when the menu can open, and none when it cannot', () => {
    for (const canManageMetadata of [true, false]) {
      const markup = row({ canManageMetadata });

      expect(markup).toContain('Model Hub'); // the row rendered at all
      expect(countTriggers(markup)).toBe(canManageMetadata ? 1 : 0);
    }
  });

  it('places the rail on the shared offsets, so the pin and menu cannot overlap', () => {
    // Pin + menu, each on its own constant — no third control, no literal offset.
    expect(railOffsets(row({ canManageMetadata: true }))).toEqual(
      [SESSION_ROW_MENU_POSITION_CLASS, SESSION_ROW_PIN_POSITION_CLASS].sort(),
    );
    // A read-only row has no rail: no pin, no menu, nothing to reserve space for.
    expect(railOffsets(row({ canManageMetadata: false }))).toEqual([]);
  });

  it('keeps the editable rail width stable while hover and focus move across rows', () => {
    const editable = row({ canManageMetadata: true });

    expect(editable).toContain('pr-11');
    expect(editable).not.toMatch(/(?:hover|focus-within|pointer-coarse):pr-/);
    expect(editable).not.toContain('padding-right');

    const readOnly = row({ canManageMetadata: false });
    expect(readOnly).toContain('pr-2.5');
  });
});

// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { createInstance } from 'i18next';
import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// A project row can outlive its project: painted from the local snapshot, or a
// live row whose archive this document missed. Its "+" then reaches a server
// that refuses with `project_archived` / `project_not_found`. The user must get
// one reason they can read, the row must go, and nothing may bring it back
// before the live list does: not the snapshot, and not a read that left the
// server before the project was archived.

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({
  apiFetch,
  isApiFetchDeadlineAbort: () => false,
  ensureCsrfToken: async () => 'csrf-not-real',
  withApiDeadline: async <T,>(run: (signal: AbortSignal) => Promise<T>) =>
    run(new AbortController().signal),
  recoverRemoteAuthFromSessionProbe: async () => {},
}));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));

import en from '../i18n/en.json';
import type { WorkbenchProject } from './ApiContext';
import { ApiProvider } from './ApiContext';
import { InstanceAuthorizationProvider } from './InstanceAuthorizationProvider';
import { useWorkbenchProjectsTree, type WorkbenchProjectsTree } from './WorkbenchProjectsContext';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { clearLocalSnapshots, setLocalSnapshotPrincipal, writeLocalSnapshot } from '../lib/localSnapshots';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

class FakeEventSource {
  readyState = 1;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {
    this.readyState = 2;
  }
}

const project = (id: string): WorkbenchProject => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: id,
  folder_path: `/tmp/${id}`,
  created_at: '2026-11-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  default_agent: null,
  capabilities: { can_chat: true, has_folder: true },
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

// The status `_coded_error_response` sends for each refusal (vibe/ui_server.py).
// The message is the server's fallback; the toast must come from this UI's catalog.
const REFUSALS = {
  project_archived: { status: 409, message: 'server fallback: archived' },
  project_not_found: { status: 404, message: 'server fallback: not found' },
} as const;

const refuse = (code: keyof typeof REFUSALS) => {
  const { status, message } = REFUSALS[code];
  return json({ ok: false, error: { code, message }, code, message }, status);
};

async function paint(...projects: WorkbenchProject[]) {
  setLocalSnapshotPrincipal('local');
  await writeLocalSnapshot('projects:bootstrap', 1, { projects });
  setLocalSnapshotPrincipal(null);
}

// Each projects bootstrap waits for the test to answer it; every create answers `create`.
function serve(create: () => Response) {
  const bootstraps: ((projects: WorkbenchProject[]) => void)[] = [];
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const url = String(path);
    if (url.startsWith('/api/workbench/projects-bootstrap')) {
      return new Promise<Response>((resolve) => {
        bootstraps.push((projects) => resolve(json({ projects, sessions: {} })));
      });
    }
    if (url === '/api/sessions' && init?.method === 'POST') return create();
    if (url.startsWith('/api/sessions?')) return json({ sessions: [], next_before_id: null });
    return json({ ok: true });
  });
  return {
    bootstrapReads: () => bootstraps.length,
    answerBootstrap: async (index: number, projects: WorkbenchProject[]) => {
      await act(async () => { bootstraps[index](projects); });
      await settle();
    },
  };
}

function mount() {
  const state = { current: null as WorkbenchProjectsTree | null };
  const Probe = ({ active }: { active: boolean }) => {
    const tree = useWorkbenchProjectsTree({ active });
    useEffect(() => { state.current = tree; });
    return null;
  };
  const tree = (active: boolean) => (
    <ApiProvider>
      <WorkbenchProjectsProvider>
        <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: 'local' }}>
          <Probe active={active} />
        </InstanceAuthorizationProvider>
      </WorkbenchProjectsProvider>
    </ApiProvider>
  );
  const view = render(tree(true));
  return Object.assign(state, { setActive: (active: boolean) => view.rerender(tree(active)) });
}

const ids = (tree: WorkbenchProjectsTree | null) => tree?.projects?.map((p) => p.id) ?? null;

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
});

beforeEach(() => {
  apiFetch.mockReset();
  showToast.mockReset();
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('a project row whose project is gone', () => {
  it.each(Object.entries(REFUSALS))(
    'answered %s on a painted row: one readable toast, the row goes, and only the live list comes back',
    async (code) => {
      await paint(project('gone'), project('kept'));
      const server = serve(() => refuse(code as keyof typeof REFUSALS));
      const state = mount();
      await settle();
      expect(ids(state.current)).toEqual(['gone', 'kept']);
      expect(server.bootstrapReads()).toBe(1);

      let created: unknown = 'pending';
      await act(async () => { created = await state.current!.createSessionForProject('gone'); });
      // Nothing to navigate to, one toast in the user's language, and the row is
      // gone before any live answer.
      expect(created).toBeNull();
      expect(showToast.mock.calls).toEqual([[en.errors[code as keyof typeof REFUSALS], 'error']]);
      expect(ids(state.current)).toEqual(['kept']);

      // The bootstrap in flight left the server before the archive and still
      // lists the project. It must not put the row back...
      await server.answerBootstrap(0, [project('gone'), project('kept')]);
      expect(ids(state.current)).toEqual(['kept']);
      // ...and the live read it is replaced by settles the list.
      expect(server.bootstrapReads()).toBe(2);
      await server.answerBootstrap(1, [project('kept')]);
      expect(ids(state.current)).toEqual(['kept']);
      expect(state.current!.projectsLoaded).toBe(true);
      expect(showToast).toHaveBeenCalledTimes(1);
    },
  );

  it('leaves a painted list it empties loading, and the snapshot does not paint the row again', async () => {
    await paint(project('gone'));
    const server = serve(() => refuse('project_archived'));
    const state = mount();
    await settle();
    expect(ids(state.current)).toEqual(['gone']);

    await act(async () => { await state.current!.createSessionForProject('gone'); });
    // Not the empty state: no live answer has said there are no projects.
    expect(state.current!.projects).toBeNull();
    // The reader leaves and comes back before the live list lands.
    state.setActive(false);
    await settle();
    state.setActive(true);
    await settle();
    expect(state.current!.projects).toBeNull();

    await server.answerBootstrap(0, [project('gone')]);
    await server.answerBootstrap(1, []);
    expect(state.current!.projects).toEqual([]);
    expect(state.current!.projectsLoaded).toBe(true);
  });

  it('re-reads the live list when the row was live and no read is in flight', async () => {
    const server = serve(() => refuse('project_archived'));
    const state = mount();
    await settle();
    await server.answerBootstrap(0, [project('gone'), project('kept')]);
    expect(ids(state.current)).toEqual(['gone', 'kept']);

    await act(async () => { await state.current!.createSessionForProject('gone'); });
    expect(ids(state.current)).toEqual(['kept']);
    await settle();
    expect(server.bootstrapReads()).toBe(2);
    await server.answerBootstrap(1, [project('kept')]);
    expect(ids(state.current)).toEqual(['kept']);
  });

  it('keeps a successful create as it was: no toast, no drop, no extra read', async () => {
    const session = { id: 'ses_new', project_id: 'kept', scope_id: 'avibe::project::kept', title: null };
    const server = serve(() => json(session, 201));
    const state = mount();
    await settle();
    await server.answerBootstrap(0, [project('kept')]);

    let created: { id: string } | null = null;
    await act(async () => { created = await state.current!.createSessionForProject('kept'); });
    await settle();
    expect(created).toMatchObject({ id: 'ses_new' });
    expect(showToast).not.toHaveBeenCalled();
    expect(ids(state.current)).toEqual(['kept']);
    expect(server.bootstrapReads()).toBe(1);
  });

  it('keeps the row through any other failure, which says nothing about the project', async () => {
    const server = serve(() => json({ ok: false, error: { code: 'internal_error', message: 'boom' } }, 500));
    const state = mount();
    await settle();
    await server.answerBootstrap(0, [project('kept')]);

    await act(async () => { await state.current!.createSessionForProject('kept'); });
    await settle();
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(ids(state.current)).toEqual(['kept']);
    expect(server.bootstrapReads()).toBe(1);
  });
});

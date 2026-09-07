// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { useWorkbenchProjectsTree } from './WorkbenchProjectsContext';
import type { WorkbenchEventHandlers, WorkbenchProject } from './ApiContext';

let handlers: WorkbenchEventHandlers;
const api = {
  getWorkbenchProjectsBootstrap: vi.fn(),
  listProjects: vi.fn(),
  reorderProjects: vi.fn(),
  updateProject: vi.fn(),
  connectWorkbenchEvents: (next: WorkbenchEventHandlers) => { handlers = next; return () => {}; },
};
vi.mock('./ApiContext', () => ({ useApi: () => api }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const rows = ['a', 'b', 'c'].map((id) => ({
  id, display_name: id, scope_id: `scope_${id}`, folder_path: `/tmp/${id}`,
  created_at: '2026-01-01T00:00:00Z', last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
})) satisfies WorkbenchProject[];
let tree: ReturnType<typeof useWorkbenchProjectsTree>;
function Probe() {
  const value = useWorkbenchProjectsTree();
  useEffect(() => { tree = value; }, [value]);
  return null;
}
const ids = () => tree.projects?.map((p) => p.id);
async function mount() {
  api.getWorkbenchProjectsBootstrap.mockResolvedValue({ projects: rows, sessions: {} });
  api.listProjects.mockResolvedValue({ projects: rows });
  render(<WorkbenchProjectsProvider><Probe /></WorkbenchProjectsProvider>);
  await waitFor(() => expect(ids()).toEqual(['a', 'b', 'c']));
}
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('holds the drag order across reads and commits only positions over a concurrent rename', async () => {
  await mount();
  let finish!: (value: { projects: WorkbenchProject[] }) => void;
  api.reorderProjects.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  let pending!: Promise<void>;
  act(() => { pending = tree.reorderProjects(['c', 'a', 'b'], ['a', 'b', 'c']); });
  expect(ids()).toEqual(['c', 'a', 'b']);
  expect(tree.isReorderingProjects).toBe(true);
  await act(async () => { handlers.onProjectsChanged?.(); });
  expect(ids()).toEqual(['c', 'a', 'b']);
  api.updateProject.mockResolvedValue({ ...rows[0], display_name: 'renamed' });
  await act(async () => { await tree.renameProject('a', 'renamed'); });
  const refreshed = [rows[2], { ...rows[0], display_name: 'renamed' }, rows[1]];
  api.listProjects.mockResolvedValue({ projects: refreshed });
  api.getWorkbenchProjectsBootstrap.mockResolvedValue({ projects: refreshed, sessions: {} });
  await act(async () => { finish({ projects: [rows[2], rows[0], rows[1]] }); await pending; });
  expect(ids()).toEqual(['c', 'a', 'b']);
  expect(tree.projects?.find((p) => p.id === 'a')?.display_name).toBe('renamed');
  expect(tree.isReorderingProjects).toBe(false);
});

it('recovers the server order after a refused save and accepts another device update', async () => {
  await mount();
  api.reorderProjects.mockRejectedValue(new Error('conflict'));
  await act(async () => { await tree.reorderProjects(['c', 'a', 'b'], ['a', 'b', 'c']); });
  await waitFor(() => expect(ids()).toEqual(['a', 'b', 'c']));
  const elsewhere = [rows[1], rows[2], rows[0]];
  api.listProjects.mockResolvedValue({ projects: elsewhere });
  api.getWorkbenchProjectsBootstrap.mockResolvedValue({ projects: elsewhere, sessions: {} });
  await act(async () => { handlers.onProjectsChanged?.(); });
  await waitFor(() => expect(ids()).toEqual(['b', 'c', 'a']));
});

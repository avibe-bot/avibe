/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const mocks = vi.hoisted(() => ({ createProject: vi.fn() }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({ useWorkbenchProjectsActions: () => mocks }));
vi.mock('../ui/folder-browser', () => ({ FolderBrowser: ({ initialPath, onSelect }: { initialPath?: string; onSelect: (path: string) => void }) => (
  <button data-testid="directory" data-path={initialPath} onClick={() => onSelect('/工作区/选择的项目')}>select-folder</button>
) }));
import { NewProjectDialog } from './NewProjectDialog';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import type { WorkbenchProject } from '../../context/ApiContext';
afterEach(cleanup);
it.each([undefined, '/工作区/当前项目'])('starts from the caller directory (%s), then reopens the selected directory', (initialPath) => {
  render(<NewProjectDialog initialPath={initialPath} onClose={() => {}} onCreated={() => {}} />);
  expect(screen.getByTestId('directory').getAttribute('data-path')).toBe(initialPath ?? null);
  fireEvent.click(screen.getByTestId('directory'));
  fireEvent.click(screen.getByText('workbench.newProjectDialog.pickFolder'));
  expect(screen.getByTestId('directory').getAttribute('data-path')).toBe('/工作区/选择的项目');
});

it.each(['project', 'null', 'unmount'] as const)('a suspended create %s delivers only to the current foreground owner', async (outcome) => {
  let resolve!: (project: WorkbenchProject | null) => void;
  mocks.createProject.mockReturnValueOnce(new Promise<WorkbenchProject | null>((done) => { resolve = done; }));
  const oldCreated = vi.fn();
  const latestCreated = vi.fn();
  const closed = vi.fn();
  const view = (active: boolean, onCreated = oldCreated) => <RouteSurfaceActiveContext.Provider value={active}>
    <NewProjectDialog onCreated={onCreated} onClose={closed} />
  </RouteSurfaceActiveContext.Provider>;
  const { rerender, unmount } = render(view(true));
  fireEvent.click(screen.getByTestId('directory'));
  fireEvent.change(screen.getByLabelText('workbench.newProjectDialog.displayName'), { target: { value: '中文项目名称' } });
  fireEvent.click(screen.getByRole('button', { name: 'workbench.newProjectDialog.create', exact: true }));
  rerender(view(false));
  if (outcome === 'unmount') unmount();
  const project = { id: 'new-project' } as WorkbenchProject;
  await act(async () => { resolve(outcome === 'null' ? null : project); });
  expect(oldCreated).not.toHaveBeenCalled();
  expect(closed).not.toHaveBeenCalled();
  if (outcome === 'unmount') return;
  rerender(view(true, latestCreated));
  await waitFor(() => expect(outcome === 'null' ? closed : latestCreated).toHaveBeenCalledTimes(1));
  rerender(view(false, latestCreated));
  rerender(view(true, latestCreated));
  expect(oldCreated).not.toHaveBeenCalled();
  expect(latestCreated).toHaveBeenCalledTimes(outcome === 'null' ? 0 : 1);
  expect(closed).toHaveBeenCalledTimes(outcome === 'null' ? 1 : 0);
});

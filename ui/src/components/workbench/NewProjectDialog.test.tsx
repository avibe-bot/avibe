/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({ useWorkbenchProjectsActions: () => ({ createProject: vi.fn() }) }));
vi.mock('../ui/directory-browser', () => ({ DirectoryBrowser: ({ initialPath, onSelect }: { initialPath?: string; onSelect: (path: string) => void }) => (
  <button data-testid="directory" data-path={initialPath} onClick={() => onSelect('/工作区/选择的项目')}>select-folder</button>
) }));
import { NewProjectDialog } from './NewProjectDialog';
afterEach(cleanup);
it.each([undefined, '/工作区/当前项目'])('starts from the caller directory (%s), then reopens the selected directory', (initialPath) => {
  render(<NewProjectDialog initialPath={initialPath} onClose={() => {}} onCreated={() => {}} />);
  expect(screen.getByTestId('directory').getAttribute('data-path')).toBe(initialPath ?? null);
  fireEvent.click(screen.getByTestId('directory'));
  fireEvent.click(screen.getByText('workbench.newProjectDialog.pickFolder'));
  expect(screen.getByTestId('directory').getAttribute('data-path')).toBe('/工作区/选择的项目');
});

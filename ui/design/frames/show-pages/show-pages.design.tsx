// Show Pages: every Session's visual page in the Library, with its visibility, link, and sharing.
import { LibraryApp } from '@/apps/LibraryApp';
import { DockProvider } from '@/context/DockProvider';

import { frames } from '../../frame';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { showPagesApi } from './fixtures';

const library = <LibraryApp initialTab="showpages" />;

// The Library window's body on a desktop; the Dock it reads comes from AppShell there.
const window = () => (
  <DockProvider enabled>
    <div className="h-full overflow-hidden">{library}</div>
  </DockProvider>
);

const designFrames = frames([
  {
    name: 'list',
    title: 'Show Pages · 资源库列表',
    width: 1000,
    height: 640,
    status: 'shipped',
    fixtures: workbenchFixtures(showPagesApi()),
    render: window,
  },
  {
    name: 'empty',
    title: 'Show Pages · 还没有页面',
    width: 1000,
    height: 480,
    status: 'shipped',
    fixtures: workbenchFixtures(showPagesApi([])),
    render: window,
  },
  {
    name: 'phone',
    title: 'Show Pages · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/apps/library',
    fixtures: workbenchFixtures(showPagesApi()),
    render: phoneShell('/apps/library', library),
  },
]);

export default designFrames;

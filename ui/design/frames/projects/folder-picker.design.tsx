// The folder picker: choose a folder on this machine for a new project (and for a Session's working directory).
import { NewProjectDialog } from '@/components/workbench/NewProjectDialog';

import { frames } from '../../frame';
import { DESIGN_NOW } from '../../shell/clock';
import { workbenchFixtures } from '../workbench/fixtures';

const HOME = '/Users/demo';
const CODE = `${HOME}/code`;
const mtime = (minutesAgo: number) => Math.floor((DESIGN_NOW - minutesAgo * 60_000) / 1000);
const dir = (name: string, minutesAgo: number) => ({ name, kind: 'dir', size: null, mtime: mtime(minutesAgo), ext: '' });

// The folder this machine's projects live in, as the Files API lists it.
const browseApi = {
  '/api/browse': { ok: true, path: CODE },
  '/api/browse/favorites': {
    ok: true,
    favorites: [
      { key: 'home', path: HOME },
      { key: 'desktop', path: `${HOME}/Desktop` },
      { key: 'documents', path: `${HOME}/Documents` },
      { key: 'downloads', path: `${HOME}/Downloads` },
    ],
  },
  '/api/files/list': {
    ok: true,
    path: CODE,
    parent: HOME,
    entries: [
      dir('avibe', 18),
      dir('avibe-bot-backend', 60 * 20),
      dir('avibe-docs', 60 * 24 * 3),
      dir('playground', 60),
      dir('_tmp', 5),
      { name: 'README.md', kind: 'file', size: 2318, mtime: mtime(60 * 24 * 12), ext: 'md' },
    ],
  },
};

const picker = () => <NewProjectDialog onClose={() => {}} onCreated={() => {}} initialPath={CODE} />;

const designFrames = frames([
  {
    name: 'new-project',
    title: '选择项目文件夹',
    width: 1100,
    height: 760,
    status: 'shipped',
    fixtures: workbenchFixtures(browseApi),
    render: picker,
  },
  {
    name: 'phone',
    title: '选择项目文件夹 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: workbenchFixtures(browseApi),
    render: picker,
  },
]);

export default designFrames;

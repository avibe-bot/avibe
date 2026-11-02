// The shipped askill registry browser, opened over the Skills page as the page opens it in the Global scope.
import { BrowseRegistryDialog } from '@/components/workbench/skills/BrowseRegistryDialog';

import { frames } from '../../frame';
import { globalChecks, globalSkills, popularSkills, skillsApi, skillsFixtures } from './fixtures';
import { skillsPage } from './page.design';

const designFrames = frames([
  {
    name: 'popular',
    title: '浏览注册表 · 热门技能',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills',
    // The dialog lists popular skills after its 250 ms search debounce.
    wait: 400,
    fixtures: skillsFixtures({
      ...skillsApi(globalSkills, globalChecks),
      '/api/skills/find?q=': { ok: true, query: '', count: popularSkills.length, skills: popularSkills },
    }),
    render: () => (
      <>
        {skillsPage()}
        <BrowseRegistryDialog
          scope="global"
          installedNames={new Set(globalSkills.map((s) => s.name))}
          onClose={() => {}}
          onInstalled={() => {}}
        />
      </>
    ),
  },
]);

export default designFrames;

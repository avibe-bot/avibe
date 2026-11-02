// The shipped Add skill dialog, opened over the Skills page as the page opens it in the Global scope.
import { AddSkillDialog } from '@/components/workbench/skills/AddSkillDialog';

import { frames } from '../../frame';
import { globalChecks, globalSkills, skillsApi, skillsFixtures } from './fixtures';
import { skillsPage } from './page.design';

const designFrames = frames([
  {
    name: 'github',
    title: '添加技能 · GitHub 链接',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(skillsApi(globalSkills, globalChecks)),
    render: () => (
      <>
        {skillsPage()}
        <AddSkillDialog defaultScope="global" onClose={() => {}} onInstalled={() => {}} />
      </>
    ),
  },
]);

export default designFrames;

// The shipped skill row in each state the Skills page draws it: installed, with an update, selected, and
// inherited from Global into a project's list.
import { SkillRow } from '@/components/workbench/skills/SkillRow';

import { frames } from '../../frame';
import { consolePageClass, skillsFixtures, globalSkills } from './fixtures';

const [pdfTools, webFetch, gitWorkflow, sentryDebug] = globalSkills;

const designFrames = frames([
  {
    name: 'states',
    title: '技能行 · 默认、有更新、选中、继承自全局',
    width: 760,
    height: 410,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: () => (
      <div className={consolePageClass}>
        <div className="flex flex-col gap-2 p-5">
          <SkillRow skill={pdfTools} />
          <SkillRow skill={webFetch} updateAvailable />
          <SkillRow skill={gitWorkflow} selected />
          <SkillRow skill={sentryDebug} inherited />
        </div>
      </div>
    ),
  },
]);

export default designFrames;

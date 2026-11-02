// The shipped project picker of the Skills page's Project scope, with its menu open.
import { ProjectPickerView } from '@/components/workbench/skills/ProjectPicker';

import { frames } from '../../frame';
import { demoProjects } from '../../shell/fixtures';
import { consolePageClass, skillsFixtures } from './fixtures';

const designFrames = frames([
  {
    name: 'open',
    title: '项目选择 · 展开',
    width: 360,
    height: 330,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: () => (
      <div className={consolePageClass}>
        <div className="p-5">
          <ProjectPickerView projects={demoProjects} value="proj_avibe" onChange={() => {}} open onOpenChange={() => {}} />
        </div>
      </div>
    ),
  },
]);

export default designFrames;

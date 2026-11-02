// The shipped skill detail panel: the 400 px column the Skills page opens beside its list when a row is selected.
import type { ReactNode } from 'react';

import { SkillDetailPanel } from '@/components/workbench/skills/SkillDetailPanel';

import { frames } from '../../frame';
import { consolePageClass, skillsFixtures, globalChecks, globalSkills, projectSkill } from './fixtures';

const webFetch = globalSkills.find((s) => s.name === 'web-fetch')!;

const panel = (node: ReactNode) => (
  <div className={consolePageClass}>
    <div className="flex w-full flex-col p-5">{node}</div>
  </div>
);

const designFrames = frames([
  {
    name: 'update',
    title: '技能详情 · 全局技能有更新',
    width: 440,
    height: 280,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: () =>
      panel(
        <SkillDetailPanel
          skill={webFetch}
          check={globalChecks.find((c) => c.name === 'web-fetch')}
          onClose={() => {}}
          onUpdate={() => {}}
          onRemove={() => {}}
        />,
      ),
  },
  {
    name: 'project',
    title: '技能详情 · 项目技能',
    width: 440,
    height: 280,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: () =>
      panel(
        <SkillDetailPanel
          skill={projectSkill('repo-conventions', '本仓库的提交、PR 与目录约定：改代码前先读它。', '0.3.0')}
          projectName="avibe"
          onClose={() => {}}
          onRemove={() => {}}
        />,
      ),
  },
]);

export default designFrames;

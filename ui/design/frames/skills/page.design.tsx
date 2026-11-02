// The shipped Skills page (Workbench · Skills & MCP, Skills tab), rendered from `/api/skills*` fixtures; the
// Project scope and an open skill come from the page's link (`?scope=project&project=…&skill=…`).
import { SkillsPage } from '@/components/workbench/SkillsPage';

import { frames } from '../../frame';
import { consoleContentClass, consolePageClass, globalChecks, globalSkills, projectSkillsApi, skillsApi, skillsFixtures } from './fixtures';

export const skillsPage = () => (
  <div className={consolePageClass}>
    <div className={consoleContentClass}>
      <SkillsPage />
    </div>
  </div>
);

const designFrames = frames([
  {
    name: 'global',
    title: '技能 · 全局技能列表',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(skillsApi(globalSkills, globalChecks)),
    render: skillsPage,
  },
  {
    name: 'selected',
    title: '技能 · 打开技能详情',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/skills?skill=web-fetch',
    fixtures: skillsFixtures(skillsApi(globalSkills, globalChecks)),
    render: skillsPage,
  },
  {
    name: 'project',
    title: '技能 · 项目范围',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/skills?scope=project&project=proj_avibe',
    fixtures: skillsFixtures(projectSkillsApi()),
    render: skillsPage,
  },
  {
    name: 'empty',
    title: '技能 · 还没有技能',
    width: 1192,
    height: 520,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(skillsApi([])),
    render: skillsPage,
  },
  {
    name: 'askill-missing',
    title: '技能 · 未安装 askill',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures({
      ...skillsApi([]),
      '/api/skills?scope=global': { ok: false, error: { code: 'askill_not_found', message: 'askill is not installed' } },
    }),
    render: skillsPage,
  },
  {
    name: 'error',
    title: '技能 · 读取失败',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures({
      ...skillsApi([]),
      '/api/skills?scope=global': { ok: false, error: { code: 'askill_failed', message: 'askill list 退出码 1：~/.agents/.skill-lock.json 不是有效的 JSON' } },
    }),
    render: skillsPage,
  },
  {
    name: 'phone',
    title: '技能 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(skillsApi(globalSkills, globalChecks)),
    render: skillsPage,
  },
  {
    name: 'selected-phone',
    title: '技能 · 手机 · 技能详情',
    width: 390,
    height: 700,
    status: 'shipped',
    route: '/skills?skill=web-fetch',
    fixtures: skillsFixtures(skillsApi(globalSkills, globalChecks)),
    render: skillsPage,
  },
]);

export default designFrames;

// The shipped Add skill dialog over the Skills page: as the Global scope opens it, after a GitHub fetch,
// and on the Upload .zip tab with a package unpacked.
import { AddSkillDialog, AddSkillDialogView, type AddSkillDialogViewProps } from '@/components/workbench/skills/AddSkillDialog';

import { frames } from '../../frame';
import { discoveredSkills, globalChecks, globalSkills, skillsApi, skillsFixtures, skillZip } from './fixtures';
import { skillsPage } from './page.design';

const fixtures = skillsFixtures(skillsApi(globalSkills, globalChecks));

const view = (props: Pick<AddSkillDialogViewProps, 'source' | 'url' | 'file' | 'uploadDir'>) => () => (
  <>
    {skillsPage()}
    <AddSkillDialogView
      {...props}
      discovered={discoveredSkills}
      // Everything found starts checked; the user left pptx-tools out.
      selected={new Set(['pdf-tools', 'docx-tools'])}
      scope="global"
      projectAvailable={false}
      busy={null}
      error={null}
      onSourceChange={() => {}}
      onUrlChange={() => {}}
      onFetch={() => {}}
      onPickFile={() => {}}
      onToggleSkill={() => {}}
      onScopeChange={() => {}}
      onInstall={() => {}}
      onClose={() => {}}
    />
  </>
);

const designFrames = frames([
  {
    name: 'github',
    title: '添加技能 · GitHub 链接',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures,
    render: () => (
      <>
        {skillsPage()}
        <AddSkillDialog defaultScope="global" onClose={() => {}} onInstalled={() => {}} />
      </>
    ),
  },
  {
    name: 'github-fetched',
    title: '添加技能 · 获取后勾选',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/skills',
    fixtures,
    render: view({ source: 'github', url: 'github.com/anthropics/skills', file: null, uploadDir: null }),
  },
  {
    name: 'zip',
    title: '添加技能 · 上传 .zip',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills',
    fixtures,
    render: view({ source: 'zip', url: '', file: skillZip(), uploadDir: '/tmp/askill-upload-q3c8x1/skill' }),
  },
]);

export default designFrames;

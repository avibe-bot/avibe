// The MCP tab of the shipped Skills & MCP page, rendered from `/api/mcp` fixtures. The tab and the open
// server come from the page's link (`?tab=mcp`, `?mcp=<name>`); the OAuth login returns to
// `/skills?mcp=<name>&oauth=success`. Dialogs are the shipped views, open over the page. The Project
// scope (`&scope=project&project=<id>`) lists that project's `.mcp.json` servers beside the global ones;
// a project login returns there with the same params.
import {
  McpAddDialog,
  McpApproveDialog,
  McpConfigureDialog,
  McpLoginDialog,
  McpRemoveDialog,
} from '@/components/workbench/skills/McpHubSection';

import { EMPTY_MCP_ADD_FORM, type McpAddFormState, type McpAddTab } from '@/lib/mcpAddForm';

import { frames } from '../../frame';
import { globalChecks, globalSkills, mcpApi, mcpServers, projectMcpApi, projectMcpServers, skillsApi, skillsFixtures } from './fixtures';
import { skillsPage } from './page.design';

const hubFixtures = (servers = mcpServers) => skillsFixtures({ ...skillsApi(globalSkills, globalChecks), ...mcpApi(servers) });
const context7 = mcpServers.find((server) => server.name === 'context7')!;
const projectRoute = '/skills?tab=mcp&scope=project&project=proj_avibe';
const projectServer = (name: string) => projectMcpServers.find((server) => server.name === name)!;
const projectFrame = (name: string, title: string, route: string, render: () => React.ReactNode) => ({
  name,
  title,
  width: 1192,
  height: 900,
  status: 'shipped' as const,
  route,
  fixtures: skillsFixtures(projectMcpApi()),
  render,
});
const pasted = `{
  "mcpServers": {
    "docs": {
      "type": "http",
      "url": "https://docs.example.com/mcp"
    }
  }
}`;

const addDialog = (tab: McpAddTab, form: McpAddFormState = EMPTY_MCP_ADD_FORM) => () => (
  <>
    {skillsPage()}
    <McpAddDialog
      open
      text={tab === 'json' ? pasted : ''}
      error={null}
      result={null}
      busy={false}
      adding={false}
      onTextChange={() => {}}
      tab={tab}
      onTabChange={() => {}}
      form={form}
      onFormChange={() => {}}
      onAdd={() => {}}
      onClose={() => {}}
      onDone={() => {}}
      onActivateLogin={() => {}}
    />
  </>
);

const addFormFrame = (name: string, title: string, form: McpAddFormState) => ({
  name,
  title,
  width: 1192,
  height: 900,
  status: 'shipped' as const,
  route: '/skills?tab=mcp',
  fixtures: hubFixtures(),
  render: addDialog('form', form),
});

const designFrames = frames([
  {
    name: 'list',
    title: 'MCP · 服务器列表',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?tab=mcp',
    fixtures: hubFixtures(),
    render: skillsPage,
  },
  {
    name: 'detail',
    title: 'MCP · 服务器详情',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?tab=mcp&mcp=context7',
    fixtures: hubFixtures(),
    render: skillsPage,
  },
  {
    name: 'login-complete',
    title: 'MCP · 登录完成回到详情',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?mcp=notion&oauth=success',
    fixtures: hubFixtures(
      mcpServers.map((server) =>
        server.name === 'notion'
          ? {
              ...server,
              auth: 'ok' as const,
              serverVersion: '1.2.0',
              toolCount: 3,
              tools: [
                { name: 'notion-search', description: '在工作区里按关键词搜索页面和数据库。' },
                { name: 'notion-fetch', description: '读取一个页面或数据库的完整内容。' },
                { name: 'notion-create-pages', description: '在指定父页面下新建页面。' },
              ],
            }
          : server,
      ),
    ),
    render: skillsPage,
  },
  {
    name: 'add',
    title: 'MCP · 添加 MCP',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?tab=mcp',
    fixtures: hubFixtures(),
    render: addDialog('json'),
  },
  addFormFrame('add-form', 'MCP · 添加 · 表单', EMPTY_MCP_ADD_FORM),
  addFormFrame('add-form-stdio', 'MCP · 表单 · stdio 命令 + 环境变量 + 高级', {
    ...EMPTY_MCP_ADD_FORM,
    name: 'filesystem',
    target: 'npx -y @modelcontextprotocol/server-filesystem "/Users/me/My Notes"',
    envRows: [{ key: 'LOG_LEVEL', value: 'info' }],
    cwd: '/Users/me',
  }),
  addFormFrame('add-form-http', 'MCP · 表单 · HTTP + Headers', {
    ...EMPTY_MCP_ADD_FORM,
    name: 'docs',
    target: 'https://docs.example.com/mcp',
    headerRows: [{ key: 'Authorization', value: 'Bearer <token>' }],
  }),
  addFormFrame('add-form-error', 'MCP · 表单 · 不支持的 shell 操作符', {
    ...EMPTY_MCP_ADD_FORM,
    name: 'tools',
    target: 'cat config.json | my-server',
  }),
  {
    name: 'remove',
    title: 'MCP · 删除确认',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?tab=mcp&mcp=context7',
    fixtures: hubFixtures(),
    render: () => (
      <>
        {skillsPage()}
        <McpRemoveDialog server={context7} busy={false} onConfirm={() => {}} onCancel={() => {}} />
      </>
    ),
  },
  {
    name: 'login-paste',
    title: 'MCP · 粘贴回调完成登录',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/skills?tab=mcp&mcp=notion',
    fixtures: hubFixtures(),
    render: () => (
      <>
        {skillsPage()}
        <McpLoginDialog
          name="notion"
          callbackText=""
          busy={false}
          onCallbackTextChange={() => {}}
          onComplete={() => {}}
          onClose={() => {}}
          onActivateLogin={() => {}}
        />
      </>
    ),
  },
  {
    name: 'empty',
    title: 'MCP · 还没有服务器',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: '/skills?tab=mcp',
    fixtures: hubFixtures([]),
    render: skillsPage,
  },
  {
    name: 'load-error',
    title: 'MCP · 读取失败',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: '/skills?tab=mcp',
    fixtures: skillsFixtures({
      ...skillsApi(globalSkills, globalChecks),
      '/api/mcp': { ok: false, error: { type: 'unavailable', message: 'Avibe service is not running' } },
    }),
    render: skillsPage,
  },
  projectFrame('project', 'MCP · 项目范围 · 各状态', projectRoute, skillsPage),
  projectFrame('project-pending', 'MCP · 项目 · 待批准详情', `${projectRoute}&mcp=repo-tools`, skillsPage),
  projectFrame('project-needs-config', 'MCP · 项目 · 需要配置详情', `${projectRoute}&mcp=sentry`, skillsPage),
  projectFrame('project-login-complete', 'MCP · 项目登录完成回到项目详情', `/skills?mcp=github&scope=project&project=proj_avibe&oauth=success`, skillsPage),
  projectFrame('approve', 'MCP · 项目 · 批准确认', `${projectRoute}&mcp=repo-tools`, () => (
    <>
      {skillsPage()}
      <McpApproveDialog server={projectServer('repo-tools')} busy={false} onConfirm={() => {}} onCancel={() => {}} />
    </>
  )),
  projectFrame('configure', 'MCP · 项目 · 配置变量与 Headers', `${projectRoute}&mcp=sentry`, () => (
    <>
      {skillsPage()}
      <McpConfigureDialog
        server={projectServer('sentry')}
        envRows={[
          { key: 'SENTRY_ORG', value: 'avibe' },
          { key: 'SENTRY_TOKEN', value: '' },
        ]}
        headerRows={[]}
        error={null}
        busy={false}
        onEnvRowsChange={() => {}}
        onHeaderRowsChange={() => {}}
        onSave={() => {}}
        onCancel={() => {}}
      />
    </>
  )),
  projectFrame('project-add', 'MCP · 项目 · 添加到 .mcp.json', projectRoute, () => (
    <>
      {skillsPage()}
      <McpAddDialog
        open
        project={{ id: 'proj_avibe', name: 'avibe' }}
        text={pasted}
        error={null}
        result={null}
        busy={false}
        adding={false}
        onTextChange={() => {}}
        tab="json"
        onTabChange={() => {}}
        form={EMPTY_MCP_ADD_FORM}
        onFormChange={() => {}}
        onAdd={() => {}}
        onClose={() => {}}
        onDone={() => {}}
        onActivateLogin={() => {}}
      />
    </>
  )),
  {
    name: 'phone',
    title: 'MCP · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/skills?tab=mcp',
    fixtures: hubFixtures(),
    render: skillsPage,
  },
]);

export default designFrames;

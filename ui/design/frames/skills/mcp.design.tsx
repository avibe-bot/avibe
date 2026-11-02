// The MCP tab of the shipped Skills & MCP page, rendered from `/api/mcp` fixtures. The tab and the open
// server come from the page's link (`?tab=mcp`, `?mcp=<name>`); the OAuth login returns to
// `/skills?mcp=<name>&oauth=success`. Dialogs are the shipped views, open over the page.
import { McpAddDialog, McpLoginDialog, McpRemoveDialog } from '@/components/workbench/skills/McpHubSection';

import { frames } from '../../frame';
import { globalChecks, globalSkills, mcpApi, mcpServers, skillsApi, skillsFixtures } from './fixtures';
import { skillsPage } from './page.design';

const hubFixtures = (servers = mcpServers) => skillsFixtures({ ...skillsApi(globalSkills, globalChecks), ...mcpApi(servers) });
const context7 = mcpServers.find((server) => server.name === 'context7')!;
const pasted = `{
  "mcpServers": {
    "docs": {
      "type": "http",
      "url": "https://docs.example.com/mcp"
    }
  }
}`;

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
    render: () => (
      <>
        {skillsPage()}
        <McpAddDialog
          open
          text={pasted}
          error={null}
          result={null}
          busy={false}
          adding={false}
          onTextChange={() => {}}
          onAdd={() => {}}
          onClose={() => {}}
          onDone={() => {}}
          onActivateLogin={() => {}}
        />
      </>
    ),
  },
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

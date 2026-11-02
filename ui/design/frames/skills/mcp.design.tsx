// The shipped MCP Hub (the MCP tab of Skills & MCP), rendered from `/api/mcp` fixtures. A server opens
// in the detail panel through the page's deep link (`?mcp=<name>`), which an OAuth login also returns to.
import { McpHubSection } from '@/components/workbench/skills/McpHubSection';

import { frames } from '../../frame';
import { consoleContentClass, consolePageClass, mcpApi, mcpServers, skillsFixtures } from './fixtures';

const hub = () => (
  <div className={consolePageClass}>
    <div className={consoleContentClass}>
      <McpHubSection />
    </div>
  </div>
);

const designFrames = frames([
  {
    name: 'list',
    title: 'MCP · 服务器列表',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    render: hub,
  },
  {
    name: 'detail',
    title: 'MCP · 服务器详情',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: { ...skillsFixtures(mcpApi(mcpServers)), search: 'mcp=context7' },
    render: hub,
  },
  {
    name: 'login-complete',
    title: 'MCP · 登录完成回到详情',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: {
      ...skillsFixtures(
        mcpApi(
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
      ),
      search: 'mcp=notion&oauth=success',
    },
    render: hub,
  },
  {
    name: 'empty',
    title: 'MCP · 还没有服务器',
    width: 1192,
    height: 420,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(mcpApi([])),
    render: hub,
  },
  {
    name: 'load-error',
    title: 'MCP · 读取失败',
    width: 1192,
    height: 420,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures({ '/api/mcp': { ok: false, error: { type: 'unavailable', message: 'Avibe service is not running' } } }),
    render: hub,
  },
  {
    name: 'phone',
    title: 'MCP · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    render: hub,
  },
]);

export default designFrames;

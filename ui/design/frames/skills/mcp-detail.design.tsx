// The shipped MCP server detail panel: the 400 px column beside the MCP list. It shows the first
// five tools and folds the rest behind one disclosure.
import { McpServerDetail } from '@/components/workbench/skills/McpHubSection';
import type { McpServerSummary } from '@/context/ApiContext';

import { frames } from '../../frame';
import { consolePageClass, mcpServerManyTools, mcpServers, skillsFixtures } from './fixtures';

const context7 = mcpServers.find((server) => server.name === 'context7')!;

const renderDetail = (server: McpServerSummary, refreshing = false) => () => (
  <div className={consolePageClass}>
    <div className="flex w-full flex-col p-5">
      <McpServerDetail
        server={server}
        canManage
        expandedTools={{}}
        busy={refreshing}
        refreshing={refreshing}
        onToggleTool={() => {}}
        onClose={() => {}}
        onRefresh={() => {}}
        onRemove={() => {}}
        onActivateLogin={() => {}}
      />
    </div>
  </div>
);

const designFrames = frames([
  {
    name: 'refreshing',
    title: 'MCP 详情 · 刷新中',
    width: 440,
    height: 480,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: renderDetail(context7, true),
  },
  {
    name: 'few-tools',
    title: 'MCP 详情 · 不超过 5 个工具，全部显示',
    width: 440,
    height: 480,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: renderDetail(context7),
  },
  {
    name: 'many-collapsed',
    title: 'MCP 详情 · 超过 5 个工具，折叠其余',
    width: 440,
    height: 640,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: renderDetail(mcpServerManyTools),
  },
  {
    name: 'many-expanded',
    title: 'MCP 详情 · 超过 5 个工具，全部展开',
    width: 440,
    height: 760,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    click: ['button[aria-expanded="false"]'],
    render: renderDetail(mcpServerManyTools),
  },
]);

export default designFrames;

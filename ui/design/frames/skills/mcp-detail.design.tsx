// The shipped MCP server detail panel: the 400 px column beside the MCP list, here mid-refresh.
import { McpServerDetail } from '@/components/workbench/skills/McpHubSection';

import { frames } from '../../frame';
import { consolePageClass, mcpServers, skillsFixtures } from './fixtures';

const context7 = mcpServers.find((server) => server.name === 'context7')!;

const designFrames = frames([
  {
    name: 'refreshing',
    title: 'MCP 详情 · 刷新中',
    width: 440,
    height: 440,
    status: 'shipped',
    fixtures: skillsFixtures({}),
    render: () => (
      <div className={consolePageClass}>
        <div className="flex w-[440px] flex-col p-5">
          <McpServerDetail
            server={context7}
            canManage
            expandedTools={{}}
            busy
            refreshing
            onToggleTool={() => {}}
            onClose={() => {}}
            onRefresh={() => {}}
            onRemove={() => {}}
            onActivateLogin={() => {}}
          />
        </div>
      </div>
    ),
  },
]);

export default designFrames;

// The Add MCP dialog: Paste JSON (default) and Form tabs. Opening states click through the shipped hub;
// filled states render the form view (`McpAddForm`, props only) from a fixture value.
import { McpAddForm } from '@/components/workbench/skills/McpAddForm';
import { McpHubSection } from '@/components/workbench/skills/McpHubSection';
import { EMPTY_MCP_ADD_FORM, type McpAddFormState } from '@/lib/mcpAddForm';

import { frames } from '../../frame';
import { consoleContentClass, consolePageClass, mcpApi, mcpServers, skillsFixtures } from './fixtures';

const hub = () => (
  <div className={consolePageClass}>
    <div className={consoleContentClass}>
      <McpHubSection />
    </div>
  </div>
);

const form = (value: McpAddFormState) => () => (
  <div className="w-[512px] rounded-lg border border-border bg-background p-6">
    <McpAddForm value={value} onChange={() => undefined} />
  </div>
);

const openAdd = 'role=button[name="{t:mcpHub.add}"]';

const designFrames = frames([
  {
    name: 'json',
    title: 'MCP · 添加 · 粘贴 JSON（默认）',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    click: [openAdd],
    render: hub,
  },
  {
    name: 'form',
    title: 'MCP · 添加 · 表单',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/skills',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    click: [openAdd, 'role=radio[name="{t:mcpHub.addModeForm}"]'],
    render: hub,
  },
  {
    name: 'form-stdio',
    title: 'MCP · 表单 · stdio 命令 + 环境变量 + 高级',
    width: 560,
    height: 520,
    status: 'shipped',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    render: form({
      ...EMPTY_MCP_ADD_FORM,
      name: 'filesystem',
      target: 'npx -y @modelcontextprotocol/server-filesystem "/Users/me/My Notes"',
      envRows: [{ key: 'LOG_LEVEL', value: 'info' }],
      cwd: '/Users/me',
    }),
  },
  {
    name: 'form-http',
    title: 'MCP · 表单 · HTTP + Headers',
    width: 560,
    height: 420,
    status: 'shipped',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    render: form({
      ...EMPTY_MCP_ADD_FORM,
      name: 'docs',
      target: 'https://example.com/mcp',
      headerRows: [{ key: 'Authorization', value: 'Bearer <token>' }],
    }),
  },
  {
    name: 'form-error',
    title: 'MCP · 表单 · 不支持的 shell 操作符',
    width: 560,
    height: 380,
    status: 'shipped',
    fixtures: skillsFixtures(mcpApi(mcpServers)),
    render: form({ ...EMPTY_MCP_ADD_FORM, name: 'tools', target: 'cat config.json | my-server' }),
  },
]);

export default designFrames;

// The Agents page's dialogs: New Agent, Run (start a chat with an Agent), and the global prompt files.
import { GlobalPromptsDialog } from '@/components/workbench/GlobalPromptsDialog';
import { NewAgentDialog } from '@/components/workbench/NewAgentDialog';
import { RunAgentDialog } from '@/components/workbench/RunAgentDialog';

import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';
import { agentsApi, demoAgents } from './fixtures';

const prompt = (backend: string, path: string, content: string) => ({
  backend,
  path,
  filename: path.split('/').pop(),
  content,
  exists: content !== '',
  read_error: false,
});

const globalPrompts = {
  backends: [
    prompt('vibey', '/Users/demo/.avibe/AGENTS.md', '# 全局约定\n\n- 中文回复，代码和命令保持原样。\n- 改动前先读仓库的 AGENTS.md。\n- 能自己验证的事情不要问我。\n'),
    prompt('claude', '/Users/demo/.claude/CLAUDE.md', '# 全局约定\n\n- 中文回复，代码和命令保持原样。\n- 改动前先读仓库的 AGENTS.md。\n'),
    prompt('codex', '/Users/demo/.codex/AGENTS.md', ''),
    prompt('opencode', '/Users/demo/.config/opencode/AGENTS.md', ''),
  ],
};

const fixtures = workbenchFixtures({ ...agentsApi(), '/api/global-prompts': globalPrompts });
const reviewer = { ...demoAgents.find((agent) => agent.name === 'reviewer')!, system_prompt: null, metadata: {}, created_at: demoAgents[0].updated_at };

const newAgent = () => <NewAgentDialog open onClose={() => {}} onCreated={() => {}} />;
const runAgent = () => <RunAgentDialog agent={reviewer} onClose={() => {}} />;
const globalPromptsDialog = () => <GlobalPromptsDialog open onClose={() => {}} />;

const designFrames = frames([
  { name: 'new-agent', title: 'Agents · 新建 Agent', width: 1000, height: 900, status: 'shipped', fixtures, render: newAgent },
  { name: 'new-agent-phone', title: 'Agents · 新建 Agent · 手机', width: 390, height: 844, status: 'shipped', fixtures, render: newAgent },
  { name: 'run-agent', title: 'Agents · 用这个 Agent 开始会话', width: 1000, height: 560, status: 'shipped', fixtures, render: runAgent },
  { name: 'run-agent-phone', title: 'Agents · 用这个 Agent 开始会话 · 手机', width: 390, height: 600, status: 'shipped', fixtures, render: runAgent },
  { name: 'global-prompts', title: 'Agents · 全局提示词', width: 1100, height: 760, status: 'shipped', fixtures, render: globalPromptsDialog },
  { name: 'global-prompts-phone', title: 'Agents · 全局提示词 · 手机', width: 390, height: 844, status: 'shipped', fixtures, render: globalPromptsDialog },
]);

export default designFrames;

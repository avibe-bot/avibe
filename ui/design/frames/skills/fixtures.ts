// Fixture builders for the Skills & MCP frames: the `/api/skills*` and `/api/mcp` reads the shipped
// page makes, served through the design network stub.
import type { McpServerSummary, SkillBrief, SkillCheckItem, SkillSearchItem } from '@/context/ApiContext';

import { DESIGN_NOW } from '../../shell/clock';
import { demoProjects, mainBot, type WorkbenchFixtures } from '../../shell/fixtures';

const at = (daysAgo: number) => new Date(DESIGN_NOW - daysAgo * 24 * 3_600_000).toISOString();

/** AppShell's console page: the console glow behind the padded content column, as `<main>` draws it. */
export const consolePageClass = 'page-glow-console h-full w-full overflow-hidden';
export const consoleContentClass = 'mx-auto w-full px-4 py-5 md:px-10 md:py-8';

const HOME = '/Users/demo';

// ── Skills ──────────────────────────────────────────────────────────────

const AGENTS = [
  { id: 'claude-code', name: 'Claude Code' },
  { id: 'codex', name: 'Codex' },
  { id: 'opencode', name: 'OpenCode' },
];

export const skill = (name: string, description: string, repo: string, version: string, extra: Partial<SkillBrief> = {}): SkillBrief => ({
  name,
  scope: 'global',
  path: `${HOME}/.agents/skills/${name}`,
  agents: AGENTS,
  description,
  version,
  sourceType: 'github',
  sourceUrl: `https://github.com/${repo}.git`,
  installSource: `gh:${repo}@${name}`,
  installedAt: at(40),
  updatedAt: at(6),
  ...extra,
});

export const globalSkills: SkillBrief[] = [
  skill('pdf-tools', '填写、合并、拆分 PDF，并抽取其中的文字与表格。', 'anthropics/skills', '1.4.0'),
  skill('web-fetch', '抓取网页并转换成干净的 Markdown。', 'avibe-bot/askill-skills', '0.9.2'),
  skill('git-workflow', '约定式提交、PR 与评审循环的完整流程。', 'obra/superpowers', '2.1.0'),
  skill('sentry-debug', '拉取 Sentry issue，按堆栈逐层定位根因。', 'getsentry/skills', '1.1.0'),
  skill('memory', '跨 Agent 会话的持久文件记忆。', 'anthropics/skills', '1.0.3'),
  skill('slack-blocks', '把结构化数据排成 Slack Block Kit 消息。', 'avibe-bot/askill-skills', '0.4.0'),
  skill('playwright-qa', '用无头浏览器跑通 Web 流程并截图验收。', 'remorses/playwriter', '0.7.1'),
];

export const projectSkill = (name: string, description: string, version: string): SkillBrief => ({
  name,
  scope: 'project',
  path: `${HOME}/avibe/.agents/skills/${name}`,
  agents: AGENTS,
  description,
  version,
  sourceType: 'local',
  installedAt: at(12),
  updatedAt: at(3),
});

/** `askill check`: web-fetch has a newer release; the rest are current. */
export const globalChecks: SkillCheckItem[] = globalSkills.map(({ name, version }) =>
  name === 'web-fetch'
    ? { name, scope: 'global', status: 'update_available', localVersion: version, remoteVersion: '0.10.0' }
    : { name, scope: 'global', status: 'up_to_date', localVersion: version, remoteVersion: version },
);

/** The reads the Skills page makes in the Global scope, answered with `skills` and their check. */
export function skillsApi(skills: SkillBrief[], checks: SkillCheckItem[] = []): Record<string, unknown> {
  return {
    '/api/projects': { projects: demoProjects },
    '/api/skills?scope=global': { ok: true, summary: { global: skills.length, project: 0 }, skills },
    '/api/skills/check?scope=global': { ok: true, skills: checks },
  };
}

export const skillsFixtures = (api: Record<string, unknown>): WorkbenchFixtures => ({ bots: [mainBot], api });

/** The avibe project's own skills; its view also lists the global ones it inherits. */
export const avibeSkills: SkillBrief[] = [
  projectSkill('repo-conventions', '本仓库的提交、PR 与目录约定：改代码前先读它。', '0.3.0'),
  projectSkill('scenario-tests', '按场景目录跑真实回归，并整理失败证据。', '1.2.0'),
  projectSkill('regression-runner', '按需拉起 Incus 回归环境，跑完自动回收。', '0.5.0'),
];

/** The reads the Skills page makes in the Project scope of the avibe project (`?scope=project&project=proj_avibe`). */
export function projectSkillsApi(): Record<string, unknown> {
  const skills = [...avibeSkills, ...globalSkills.slice(0, 2)];
  return {
    ...skillsApi(globalSkills, globalChecks),
    '/api/skills?scope=all&project_id=proj_avibe': { ok: true, summary: { global: 2, project: avibeSkills.length }, skills },
    '/api/skills/check?scope=project&project_id=proj_avibe': {
      ok: true,
      skills: avibeSkills.map(({ name, version }) => ({ name, scope: 'project', status: 'up_to_date', localVersion: version, remoteVersion: version })),
    },
  };
}

/** A .zip the Add dialog's upload tab holds, at a realistic size. */
export const skillZip = () => new File([new Uint8Array(240 * 1024)], 'pdf-toolkit.zip', { type: 'application/zip' });

/** What fetching `anthropics/skills` (or unpacking pdf-toolkit.zip) finds. */
export const discoveredSkills = [
  { name: 'pdf-tools', description: '填写、合并、拆分 PDF，并抽取其中的文字与表格。' },
  { name: 'docx-tools', description: '读写 Word 文档，保留样式与批注。' },
  { name: 'pptx-tools', description: '从 Markdown 大纲生成幻灯片。' },
];

export const registryItem = (
  id: number,
  name: string,
  description: string,
  repo: string,
  aiScore: number,
  stars: number,
  tags: string[],
): SkillSearchItem => {
  const [owner, repoName] = repo.split('/');
  return {
    id,
    name,
    description,
    owner,
    repo: repoName,
    tags,
    stars,
    aiScore,
    aiBreakdown: [],
    updatedAt: at(5),
    installSource: `gh:${repo}@${name}`,
    url: `https://askill.sh/skills/${id}`,
  };
};

/** `askill find` with an empty query: the popular skills, ranked by AI score. */
export const popularSkills: SkillSearchItem[] = [
  registryItem(52912, 'pdf-tools', '填写、合并、拆分 PDF，并抽取其中的文字与表格。', 'anthropics/skills', 96, 12034, ['documents', 'pdf']),
  registryItem(41207, 'code-review', '对抗式 diff 评审，带通过 / 不通过的质量闸门。', 'obra/superpowers', 95, 4812, ['review', 'git']),
  registryItem(60318, 'pr-describe', '根据 diff 生成 PR 标题和说明。', 'anthropics/skills', 92, 9870, ['git', 'review']),
  registryItem(38455, 'security-review', '标出改动里的不安全写法：注入、越权、明文密钥。', 'semgrep/skills', 91, 6120, ['security', 'review']),
  registryItem(47702, 'lint-fix', '对整份 diff 运行并自动修复 lint 问题。', 'biomejs/skills', 87, 1930, ['lint', 'ci']),
  registryItem(55890, 'sql-runner', '只读连接数据库，把自然语言问题翻成 SQL 并给出结果表。', 'duckdb/skills', 83, 2210, ['data']),
];

// ── MCP ─────────────────────────────────────────────────────────────────

export const mcpServer = (name: string, extra: Partial<McpServerSummary>): McpServerSummary => ({
  name,
  transport: 'streamable_http',
  source: 'avibe',
  auth: 'none',
  generatedAt: '2026-10-31T10:57:12+00:00',
  toolCount: extra.tools?.length ?? 0,
  ...extra,
});

export const mcpServers: McpServerSummary[] = [
  mcpServer('context7', {
    source: 'claude',
    sourcePath: '/Users/demo/.claude.json',
    url: 'https://mcp.context7.com/mcp',
    serverVersion: '1.0.14',
    tools: [
      { name: 'resolve-library-id', description: '把库名解析成 Context7 可用的库 ID，返回最匹配的几个候选及其文档覆盖度。' },
      {
        name: 'get-library-docs',
        description:
          '拉取指定库的最新文档。可以按主题（例如 routing、hooks）收窄范围，并用 tokens 限制返回长度；库 ID 需先用 resolve-library-id 解析，除非用户已经给出 /org/project 形式的 ID。',
      },
    ],
  }),
  mcpServer('github', {
    transport: 'stdio',
    command: 'npx -y @modelcontextprotocol/server-github',
    serverVersion: '2025.4.8',
    toolCount: 26,
  }),
  mcpServer('notion', { url: 'https://mcp.notion.com/mcp', auth: 'needs_login', toolCount: 0 }),
  mcpServer('blender', { source: 'codex', sourcePath: '/Users/demo/.codex/config.toml', transport: 'stdio', command: 'uvx blender-mcp', toolCount: 9 }),
  mcpServer('exa', { source: 'claude', sourcePath: '/Users/demo/.claude.json', url: 'https://mcp.exa.ai/mcp', toolCount: 2 }),
  mcpServer('node_repl', { source: 'codex', sourcePath: '/Users/demo/.codex/config.toml', transport: 'stdio', command: 'node ~/.codex/mcp/node-repl.js', toolCount: 4 }),
  mcpServer('linear', { source: 'opencode', sourcePath: '/Users/demo/.config/opencode/opencode.json', url: 'https://mcp.linear.app/mcp', toolCount: 23 }),
];

export const mcpApi = (servers: McpServerSummary[]): Record<string, unknown> => ({
  '/api/mcp': { ok: true, servers, oauthMode: 'direct' },
});

const PROJECT_MCP_FILE = '/Users/demo/avibe/.mcp.json';

/** A row from the avibe project's `.mcp.json`, in the state `/api/mcp?project=proj_avibe` reports. */
export const projectMcpServer = (name: string, state: string, extra: Partial<McpServerSummary>): McpServerSummary => ({
  name,
  source: 'project',
  scope: 'project:proj_avibe',
  sourcePath: PROJECT_MCP_FILE,
  state,
  auth: state === 'needs_login' ? 'needs_login' : 'none',
  transport: 'http',
  missingVariables: [],
  placeholders: [],
  toolCount: extra.tools?.length ?? 0,
  tools: [],
  ...extra,
});

/** The avibe project's view: its own servers (one per state) shadowing and beside the visible global ones. */
export const projectMcpServers: McpServerSummary[] = [
  projectMcpServer('repo-tools', 'pending_approval', { transport: 'stdio', command: 'node ./scripts/mcp/repo-tools.js', placeholders: [] }),
  projectMcpServer('sentry', 'needs_config', {
    url: 'https://mcp.sentry.dev/mcp?org=${SENTRY_ORG}',
    missingVariables: ['SENTRY_ORG', 'SENTRY_TOKEN'],
    placeholders: ['SENTRY_ORG', 'SENTRY_TOKEN'],
  }),
  projectMcpServer('linear', 'needs_login', { url: 'https://mcp.linear.app/mcp' }),
  projectMcpServer('github', 'ok', {
    transport: 'stdio',
    command: 'npx -y @modelcontextprotocol/server-github',
    placeholders: ['GITHUB_TOKEN'],
    description: '本仓库的 issue、PR 与代码搜索。',
    revision: 'r3',
    tools: [
      { name: 'search_issues', description: '按关键词在本仓库搜索 issue。' },
      { name: 'get_pull_request', description: '读取一个 PR 的描述、评审与检查状态。' },
    ],
  }),
  projectMcpServer('staging-db', 'not_generated', { transport: 'stdio', command: 'uvx postgres-mcp --read-only' }),
  ...mcpServers.filter((server) => !['github', 'linear'].includes(server.name)).map((server) => ({ ...server, scope: 'global', state: server.auth === 'needs_login' ? 'needs_login' : 'ok' })),
].sort((a, b) => a.name.localeCompare(b.name));

/** The Skills & MCP reads of the avibe project's MCP view (`?tab=mcp&scope=project&project=proj_avibe`). */
export function projectMcpApi(servers: McpServerSummary[] = projectMcpServers): Record<string, unknown> {
  return {
    ...projectSkillsApi(),
    ...mcpApi(mcpServers),
    '/api/mcp?project=proj_avibe': { ok: true, servers, oauthMode: 'direct' },
  };
}

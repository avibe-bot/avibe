import type { SkillScope } from '../../../context/ApiContext';

export type SkillsTab = 'skills' | 'mcp';

/** The Skills & MCP view a link asks for. Without a query it is the page's default view. */
export interface SkillsLink {
  tab: SkillsTab;
  scope: SkillScope;
  /** A project id; null keeps the page's own choice (the first project). */
  project: string | null;
  /** A skill name to open in the detail panel. */
  skill: string | null;
}

// `?tab=mcp`, `?scope=project`, `?project=<id>` and `?skill=<name>` are destinations, as on the Agents
// and Harness pages: the page reads them one way and the user's own clicks never write them back. A
// bare `?mcp=<name>` opens the MCP tab too, because that is all the MCP OAuth login returns with
// (`/skills?mcp=<name>&oauth=success`); McpHubSection selects the server.
export function skillsLinkFromParams(params: URLSearchParams): SkillsLink {
  const tab = params.get('tab');
  return {
    tab: tab === 'mcp' || (tab !== 'skills' && Boolean(params.get('mcp'))) ? 'mcp' : 'skills',
    scope: params.get('scope') === 'project' ? 'project' : 'global',
    project: params.get('project') || null,
    skill: params.get('skill') || null,
  };
}

/** An `/api/mcp…` path; a project view adds `?project=<id>` so the call resolves names in that project. */
export function mcpPath(suffix: string, project?: string | null): string {
  return `/api/mcp${suffix}${project ? `?project=${encodeURIComponent(project)}` : ''}`;
}

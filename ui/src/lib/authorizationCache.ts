const AUTHORIZATION_SENSITIVE_READ_PREFIXES = [
  '/api/projects',
  '/api/workbench/projects-bootstrap',
  '/api/sessions',
  '/api/inbox',
  '/api/search',
  '/api/show-pages',
  '/api/agents',
  '/api/skills',
  '/api/vault/',
] as const;

export function isAuthorizationSensitiveReadPath(path: string): boolean {
  return AUTHORIZATION_SENSITIVE_READ_PREFIXES.some((prefix) =>
    path.startsWith(prefix),
  );
}

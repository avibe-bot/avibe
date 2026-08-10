export type InstanceCapabilities = {
  is_instance_owner: boolean;
  can_read_instance: boolean;
  can_chat: boolean;
  can_manage_projects: boolean;
  can_manage_agents: boolean;
  can_manage_instance: boolean;
  can_use_agents: boolean;
  can_use_skills: boolean;
  can_use_vault_secrets: boolean;
  can_use_show_pages: boolean;
  can_use_terminal_files: boolean;
  can_use_terminal: boolean;
  can_use_files: boolean;
  can_use_system: boolean;
};

export type SessionInfo =
  | { remote: false; instance_role?: 'owner'; capabilities?: InstanceCapabilities }
  | { remote: true; authenticated: false; authorization_refresh_required?: boolean }
  | {
      remote: true;
      authenticated: true;
      email: string;
      sub?: string;
      instance_role: 'owner' | 'editor' | 'viewer';
      capabilities: InstanceCapabilities;
    };

export const DENIED_INSTANCE_CAPABILITIES: InstanceCapabilities = {
  is_instance_owner: false,
  can_read_instance: false,
  can_chat: false,
  can_manage_projects: false,
  can_manage_agents: false,
  can_manage_instance: false,
  can_use_agents: false,
  can_use_skills: false,
  can_use_vault_secrets: false,
  can_use_show_pages: false,
  can_use_terminal_files: false,
  can_use_terminal: false,
  can_use_files: false,
  can_use_system: false,
};

export const OWNER_INSTANCE_CAPABILITIES: InstanceCapabilities = Object.fromEntries(
  Object.keys(DENIED_INSTANCE_CAPABILITIES).map((key) => [key, true]),
) as InstanceCapabilities;

export const canCreateLocalProject = (capabilities: InstanceCapabilities): boolean =>
  capabilities.can_manage_projects && capabilities.can_use_files;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const normalizeCapabilities = (value: Record<string, unknown>): InstanceCapabilities =>
  Object.fromEntries(
    Object.keys(DENIED_INSTANCE_CAPABILITIES).map((key) => [key, value[key] === true]),
  ) as InstanceCapabilities;

export const normalizeSessionInfo = (value: unknown): SessionInfo => {
  if (!isRecord(value)) return { remote: true, authenticated: false };

  if (value.remote === false) {
    return {
      remote: false,
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    };
  }

  if (value.remote !== true || value.authenticated !== true) {
    return {
      remote: true,
      authenticated: false,
      ...(value.authorization_refresh_required === true
        ? { authorization_refresh_required: true }
        : {}),
    };
  }

  const rawCapabilities = isRecord(value.capabilities) ? value.capabilities : null;
  // Releases before instance roles treated every authenticated remote user as
  // the instance owner and did not return a capabilities object.
  const capabilities = rawCapabilities
    ? normalizeCapabilities(rawCapabilities)
    : OWNER_INSTANCE_CAPABILITIES;
  const instanceRole =
    value.instance_role === 'owner' ||
    value.instance_role === 'editor' ||
    value.instance_role === 'viewer'
      ? value.instance_role
      : rawCapabilities && !capabilities.is_instance_owner
        ? 'viewer'
        : 'owner';

  return {
    remote: true,
    authenticated: true,
    email: typeof value.email === 'string' ? value.email : '',
    ...(typeof value.sub === 'string' ? { sub: value.sub } : {}),
    instance_role: instanceRole,
    capabilities,
  };
};

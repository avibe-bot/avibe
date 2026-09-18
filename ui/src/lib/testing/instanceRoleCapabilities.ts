/**
 * Test-only mirror of the server capability projection
 * (`AuthorizationContext.capability_projection` in `vibe/authorization.py`).
 *
 * The Web UI never derives capabilities from a role — the server projects them
 * and `normalizeSessionInfo` only copies the bits through — so a component test
 * that renders "an Editor" has to state the same bits the server would have
 * sent. Stating them once keeps every permission test arguing against one
 * description of the roles instead of four hand-written ones.
 *
 * `tests/test_instance_authorization.py` is the anchor: it freezes the viewer
 * and editor rows bitwise and defines member as owner minus member management.
 * A capability added there must be added here.
 */
import {
  DENIED_INSTANCE_CAPABILITIES,
  OWNER_INSTANCE_CAPABILITIES,
  type InstanceCapabilities,
  type InstanceRole,
} from '../sessionInfo';

/** Read the instance and open Show Pages; no chat and no resource use. */
const VIEWER_CAPABILITIES: InstanceCapabilities = {
  ...DENIED_INSTANCE_CAPABILITIES,
  can_read_instance: true,
  can_use_show_pages: true,
};

/** Viewer plus the use authorities: chat, Agents, Skills, Vault, files, terminal.
 *  Every `can_manage_*` bit stays false — using a resource is not managing it. */
const EDITOR_CAPABILITIES: InstanceCapabilities = {
  ...VIEWER_CAPABILITIES,
  can_chat: true,
  can_use_agents: true,
  can_use_skills: true,
  can_use_vault_secrets: true,
  can_use_terminal_files: true,
  can_use_terminal: true,
  can_use_files: true,
};

/** Owner minus member management and the owner flag itself. */
const MEMBER_CAPABILITIES: InstanceCapabilities = {
  ...OWNER_INSTANCE_CAPABILITIES,
  is_instance_owner: false,
  can_manage_access_members: false,
};

export const ROLE_CAPABILITIES: Record<InstanceRole, InstanceCapabilities> = {
  viewer: VIEWER_CAPABILITIES,
  editor: EDITOR_CAPABILITIES,
  member: MEMBER_CAPABILITIES,
  owner: OWNER_INSTANCE_CAPABILITIES,
};

export const capabilitiesFor = (role: InstanceRole): InstanceCapabilities => ROLE_CAPABILITIES[role];

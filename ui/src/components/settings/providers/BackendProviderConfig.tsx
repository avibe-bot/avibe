import type { BackendId } from '../shared/useBackendRuntime';
import { ClaudeProviderConfig } from './ClaudeProviderConfig';
import { CodexProviderConfig } from './CodexProviderConfig';
import { OpencodeProviderConfig } from './OpencodeProviderConfig';

/**
 * Dispatches to the backend-specific config body. The same component
 * tree is rendered by the settings route (wrapped in ``SettingsPageShell``)
 * and the setup wizard modal, so neither callsite copies the per-backend
 * logic.
 */
export function BackendProviderConfig({
  backend,
  hideEnableToggle,
}: {
  backend: BackendId;
  hideEnableToggle?: boolean;
}) {
  switch (backend) {
    case 'claude':
      return <ClaudeProviderConfig hideEnableToggle={hideEnableToggle} />;
    case 'codex':
      return <CodexProviderConfig hideEnableToggle={hideEnableToggle} />;
    case 'opencode':
      return <OpencodeProviderConfig hideEnableToggle={hideEnableToggle} />;
    default:
      return null;
  }
}

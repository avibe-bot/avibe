// How a vault request names the session that asked for it. Pure projection of the
// request payload — the card and the Vaults page render the same answer, and
// `vault-request-session-link.tsx` only decides how to link it.
import type { VaultRequest } from '@/context/ApiContext';

export type VaultRequestSessionDisplay = {
  id: string;
  label: string;
  isWorkbench: boolean;
  isIdFallback: boolean;
};

export function vaultRequestSessionDisplay(request: VaultRequest): VaultRequestSessionDisplay | null {
  const card = (request.card ?? {}) as { session_id?: unknown };
  const cardSessionId = typeof card.session_id === 'string' && card.session_id.trim() ? card.session_id.trim() : null;
  const session = request.session ?? null;
  const id = session?.id?.trim() || cardSessionId;
  if (!id) return null;
  const title = session?.title?.trim();
  const sessionLabel = session?.label?.trim();
  const label = title || sessionLabel || id;
  return {
    id,
    label,
    isWorkbench: Boolean(session?.is_workbench),
    isIdFallback: label === id && !title && (!sessionLabel || sessionLabel === id),
  };
}

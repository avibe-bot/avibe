import { RefreshCw, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { OpencodePermissionState } from './useOpencodePermission';

export interface OpencodePermissionSetupProps {
  /** Whether the OpenCode CLI is detected/ready — the affordance only makes
   * sense once the binary exists. */
  cliReady: boolean;
  /** True once opencode.json already grants ``permission: "allow"``. */
  permissionAllowed: boolean;
  state: OpencodePermissionState;
  message: string;
  onSetup: () => void;
  className?: string;
}

/**
 * The shared "Allow tool calls" callout for OpenCode, rendered identically by
 * the Settings provider page and the setup wizard. Without
 * ``permission: "allow"`` the OpenCode daemon prompts on every tool call and
 * avibe can't answer — so this surfaces a prominent write-allow button.
 *
 * Renders nothing once the CLI isn't ready or permission is already granted: a
 * permanent "Setup permission" button is misleading once opencode.json already
 * carries the value.
 */
export function OpencodePermissionSetup({
  cliReady,
  permissionAllowed,
  state,
  message,
  onSetup,
  className,
}: OpencodePermissionSetupProps) {
  const { t } = useTranslation();
  if (!cliReady || permissionAllowed) return null;
  return (
    <div className={cn('rounded-lg border border-gold/30 bg-gold/10 px-3 py-2.5', className)}>
      <p className="mb-2 text-[12px] text-gold-ink">{t('agentDetection.permissionHintStrong')}</p>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="brand-gold"
          size="xs"
          onClick={onSetup}
          disabled={state === 'loading'}
        >
          {state === 'loading' ? (
            <RefreshCw className="size-3.5 animate-spin" />
          ) : (
            <Settings className="size-3.5" />
          )}
          {t('agentDetection.setupPermission')}
        </Button>
        {state === 'success' && <span className="text-[12px] text-mint-ink">{message}</span>}
        {state === 'error' && <span className="text-[12px] text-destructive-ink">{message}</span>}
      </div>
    </div>
  );
}

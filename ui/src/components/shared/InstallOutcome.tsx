import { ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { InstallResult } from '../../context/ApiContext';
import { cn } from '@/lib/utils';

export type InstallOutcomeResult = Pick<InstallResult, 'ok' | 'message' | 'output' | 'hint' | 'exit_code'>;

/**
 * How an agent CLI install or upgrade ended: the server's localized headline, its
 * hint when it recognized the failure, and the installer's own output behind a
 * disclosure. Every surface that runs one draws the result through here, so a
 * failure reads the same in Settings, in setup and in the lifecycle popover, and
 * stays on screen after the toast is gone.
 *
 * Text size and the surrounding tone belong to the host; the hint inherits it.
 */
export function InstallOutcome({ result, className }: { result: InstallOutcomeResult; className?: string }) {
  const { t } = useTranslation();
  const { ok, message, hint, output } = result;
  const exitCode = typeof result.exit_code === 'number' ? result.exit_code : null;
  if (!message && !output) return null;
  return (
    <div className={cn('min-w-0 space-y-1 break-words', className)} role={ok ? 'status' : 'alert'}>
      {message && <p className={ok ? 'text-mint-ink' : 'text-destructive-ink'}>{message}</p>}
      {hint && <p>{hint}</p>}
      {(output || exitCode !== null) && (
        <details className="group">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-cyan-ink hover:text-cyan-ink/80">
            <ChevronDown size={12} className="transition-transform group-open:rotate-180" />
            {t('agentDetection.showOutput')}
          </summary>
          {exitCode !== null && (
            <p className="mt-2 font-mono text-muted">{t('agentDetection.exitCode', { code: exitCode })}</p>
          )}
          {output && (
            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded border border-border bg-background px-3 py-2 font-mono text-[11px] text-muted">
              {output}
            </pre>
          )}
        </details>
      )}
    </div>
  );
}

import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  ChevronDown,
  ChevronUp,
  Download,
  RefreshCw,
  Save,
  Search,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { BackendLifecycleChip } from '../BackendLifecycleChip';
import { ToggleSwitch } from '../SettingsPrimitives';
import type { BackendRuntimeState, BackendId } from './useBackendRuntime';

export interface BackendRuntimeCardProps {
  /** Backend id used by ``BackendLifecycleChip`` + ``installAgent``. */
  backend: BackendId;
  /** Display name shown in the card header (e.g. "Claude Code"). */
  label: string;
  /** Short description sentence under the name. */
  description: string;
  /** Lucide icon component used in the header tile. */
  Icon: LucideIcon;
  /**
   * Tailwind class string for the icon tile background, e.g.
   * ``"bg-cyan-soft"`` (Claude) / ``"bg-gold"`` (Codex) /
   * ``"bg-violet-soft"`` (OpenCode). Keeps brand colour consistent
   * with the icon-only chip used elsewhere in the design.
   */
  iconTileClassName: string;
  /** Tailwind class for the icon glyph colour, e.g. ``"text-cyan-ink"``. */
  iconClassName: string;
  /** Runtime state object from ``useBackendRuntime``. */
  runtime: BackendRuntimeState;
  /**
   * Optional extra row rendered between the install-hint and the Save
   * button. OpenCode uses this for the ``permission: allow`` affordance;
   * Claude / Codex don't supply one.
   */
  extraSlot?: React.ReactNode;
  /**
   * Hide the enable ``ToggleSwitch`` in the header. Used by the setup
   * wizard, where each backend card already owns a separate enable
   * toggle (local wizard state) and the embedded provider config must
   * not render a second, immediately-persisting control that diverges
   * from it. Defaults to ``false`` so the settings route keeps the
   * toggle exactly as before.
   */
  hideEnableToggle?: boolean;
}

/**
 * Settings → Backends Runtime card. Identical visual layout across
 * Claude / Codex / OpenCode pages — previously copy-pasted; lifted
 * here once it became clear new backends inherit the affordances for
 * free instead of needing a third clone (the omission of this card
 * from the Codex page in #282 was the trigger).
 *
 * The component owns no state — pages pass a ``BackendRuntimeState``
 * from ``useBackendRuntime`` and the chip's ``onChanged`` callback is
 * already wired by the hook. The Runtime card stays purely visual so
 * pages remain free to compose it alongside any auth / providers /
 * test panels below.
 */
export const BackendRuntimeCard: React.FC<BackendRuntimeCardProps> = ({
  backend,
  label,
  description,
  Icon,
  iconTileClassName,
  iconClassName,
  runtime,
  extraSlot,
  hideEnableToggle,
}) => {
  const { t } = useTranslation();
  const inputId = `${backend}-cli-path`;

  return (
    <Card>
      <CardContent className="flex flex-col gap-5 p-6">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <div
              className={clsx(
                'flex size-11 shrink-0 items-center justify-center rounded-[10px]',
                iconTileClassName,
              )}
            >
              <Icon size={22} className={iconClassName} />
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[15px] font-semibold text-foreground">{label}</span>
              <span className="text-[12px] text-muted">{description}</span>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <BackendLifecycleChip
              name={backend}
              enabled={runtime.enabled}
              cliStatus={runtime.cliStatus}
              onChanged={runtime.handleLifecycleChanged}
            />
            {!hideEnableToggle && (
              <ToggleSwitch enabled={runtime.enabled} onClick={runtime.toggleEnabled} />
            )}
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor={inputId} className="text-xs font-medium uppercase text-muted">
            {t('agentDetection.cliPath')}
          </Label>
          <div className="flex gap-2">
            <Input
              id={inputId}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('agentDetection.cliPathPlaceholder', { name: backend }) as string}
              value={runtime.cliPath}
              onChange={(e) => runtime.setCliPath(e.target.value)}
              className="font-mono"
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => void runtime.detect(runtime.cliPath)}
              disabled={runtime.detecting}
            >
              {runtime.detecting ? (
                <RefreshCw className="size-3.5 animate-spin" />
              ) : (
                <Search className="size-3.5" />
              )}
              {t('common.detect')}
            </Button>
          </div>
          <p className="text-[12px] text-muted">{t('settings.backends.cliPathHint')}</p>
        </div>

        {runtime.cliStatus === 'missing' && (
          <div className="space-y-2 rounded-lg border border-cyan/30 bg-cyan/[0.06] px-3 py-2.5">
            <p className="text-[12px] text-cyan-ink">{t('agentDetection.installHint')}</p>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="brand-cyan"
                size="xs"
                onClick={() => void runtime.install()}
                disabled={runtime.installing}
              >
                {runtime.installing ? (
                  <RefreshCw className="size-3.5 animate-spin" />
                ) : (
                  <Download className="size-3.5" />
                )}
                {runtime.installing
                  ? t('agentDetection.installing')
                  : t('agentDetection.installAgent')}
              </Button>
              {runtime.installResult?.message && (
                <span
                  className={clsx(
                    'text-[12px]',
                    runtime.installResult.ok ? 'text-mint-ink' : 'text-destructive-ink',
                  )}
                >
                  {runtime.installResult.message}
                </span>
              )}
            </div>
            {runtime.installResult?.output && (
              <div>
                <button
                  type="button"
                  onClick={() => runtime.setInstallOutputOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-[11px] text-cyan-ink transition hover:text-cyan-ink/80"
                >
                  {runtime.installOutputOpen ? (
                    <ChevronUp size={12} />
                  ) : (
                    <ChevronDown size={12} />
                  )}
                  {t('agentDetection.showOutput')}
                </button>
                {runtime.installOutputOpen && (
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded border border-border bg-background px-3 py-2 font-mono text-[11px] text-muted">
                    {runtime.installResult.output}
                  </pre>
                )}
              </div>
            )}
          </div>
        )}

        {extraSlot}

        {runtime.runtimeDirty && (
          <div className="flex justify-end">
            <Button
              variant="brand"
              size="default"
              onClick={() => void runtime.onSaveRuntime()}
              disabled={runtime.savingRuntime}
            >
              <Save className="size-3.5" />
              {runtime.savingRuntime ? t('common.saving') : t('common.save')}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
};

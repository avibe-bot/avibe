import React, { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  RefreshCw,
  Sliders,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { BackendIcon, EyebrowBadge, WizardCard } from '../visual';
import type { BackendId } from '../visual';
import { BackendLifecycleChip } from '../settings/BackendLifecycleChip';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { BackendProviderConfig } from '../settings/providers/BackendProviderConfig';
import { OpencodePermissionSetup } from '../settings/shared/OpencodePermissionSetup';
import type { BackendId as RuntimeBackendId } from '../settings/shared/useBackendRuntime';
import { useOpencodePermission } from '../settings/shared/useOpencodePermission';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { DEFAULT_AGENT_STATE, getBackendUiMeta } from '@/lib/agentBackends';

interface AgentDetectionProps {
  data: any;
  onNext: (data: any) => void;
  onBack?: () => void;
  isPage?: boolean;
  onSave?: (data: { agents: Record<string, AgentState> }) => Promise<void> | void;
}

type AgentState = {
  enabled: boolean;
  cli_path: string;
  status?: 'unknown' | 'ok' | 'missing';
};

const DEFAULT_AGENTS = DEFAULT_AGENT_STATE as Record<string, AgentState>;

// Backends with a dedicated provider config body (rendered in the wizard
// modal / the settings route). Mirrors ``BackendProviderConfig``'s switch —
// anything outside this set has no provider UI to configure.
const PROVIDER_BACKENDS: ReadonlySet<string> = new Set(['claude', 'codex', 'opencode']);

const normalizeAgents = (source: any): Record<string, AgentState> => {
  const raw = source?.agents || {};
  return Object.fromEntries(
    Object.entries(DEFAULT_AGENTS).map(([name, fallback]) => {
      const next = raw?.[name] || {};
      return [
        name,
        {
          ...fallback,
          ...next,
          status: next.status || fallback.status,
        },
      ];
    })
  );
};

// Mirrors design.pen JHgjz (Backends wizard step) and qVHh4 (Settings → Backends).
// Each backend renders as a two-row card: a header row (icon, label, one-line
// description, status pill, enable switch) and an action row (configure
// provider / set up Allow / install). Detection runs automatically on mount —
// the user enables what they have and installs anything missing.
export const AgentDetection: React.FC<AgentDetectionProps> = ({ data, onNext, onBack, isPage = false, onSave }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [agents, setAgents] = useState<Record<string, AgentState>>(normalizeAgents(data));
  const permission = useOpencodePermission({ autoFetchStatus: true });
  const [installingAgents, setInstallingAgents] = useState<Record<string, boolean>>({});
  const [installResults, setInstallResults] = useState<
    Record<string, { ok: boolean; message: string; output?: string | null }>
  >({});
  const [expandedOutputs, setExpandedOutputs] = useState<Record<string, boolean>>({});
  // Which backend's "Configure provider" modal is open (wizard mode only).
  const [providerModal, setProviderModal] = useState<string | null>(null);
  // True while a provider-modal close is reloading config into ``agents``; the
  // promise lets handlePrimaryAction wait for and fold in that reload.
  const [syncing, setSyncing] = useState(false);
  const syncRef = useRef<Promise<Record<string, AgentState> | null> | null>(null);
  const isMissing = (agent: AgentState) => agent.status === 'missing';

  const isAnyInstalling = Object.values(installingAgents).some(Boolean);

  useEffect(() => {
    detectAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const detect = async (name: string, binary?: string) => {
    const result = await api.detectCli(binary || name);
    setAgents((prev) => ({
      ...prev,
      [name]: {
        ...prev[name],
        cli_path: result.path || prev[name].cli_path,
        status: result.found ? 'ok' : 'missing',
      },
    }));
  };

  const detectAll = async () => {
    await Promise.all(Object.entries(agents).map(([name, agent]) => detect(name, agent.cli_path)));
  };

  // Re-read persisted config after the provider modal closes so runtime edits
  // made inside it (enabled / cli_path via useBackendRuntime) flow back into the
  // wizard's local ``agents`` state — otherwise handlePrimaryAction would save a
  // stale snapshot and clobber them. Then re-detect to refresh the status pill.
  const syncBackendFromConfig = async (name: string): Promise<Record<string, AgentState> | null> => {
    setSyncing(true);
    let cliPath = agents[name]?.cli_path;
    let synced: Record<string, AgentState> | null = null;
    try {
      const config = await api.getConfig();
      const saved = config?.agents?.[name];
      if (saved) {
        if (typeof saved.cli_path === 'string' && saved.cli_path) {
          cliPath = saved.cli_path;
        }
        // Spread the full persisted backend so provider-level fields the modal
        // may have changed (e.g. opencode default_provider / default_model) are
        // refreshed too — not just enabled / cli_path.
        const merged: AgentState = {
          ...agents[name],
          ...saved,
          cli_path: cliPath || agents[name].cli_path,
        };
        // ``enabled`` is owned by the live card toggle, never this async sync's
        // snapshot: closing the provider modal kicks off this sync, but the user
        // may flip the toggle before it resolves. Apply the *live* enable state
        // at both consumers — ``prev`` here, and the live ``agents`` in
        // handlePrimaryAction — so a just-flipped toggle is never reverted.
        synced = { [name]: merged };
        setAgents((prev) => ({
          ...prev,
          [name]: { ...prev[name], ...merged, enabled: prev[name].enabled },
        }));
      }
      await detect(name, cliPath);
    } catch {
      // Best-effort: fall back to whatever we already have.
    } finally {
      setSyncing(false);
    }
    return synced;
  };

  const toggle = (name: string, enabled: boolean) => {
    setAgents((prev) => ({
      ...prev,
      [name]: { ...prev[name], enabled },
    }));
  };

  const installAgent = async (name: string) => {
    if (isAnyInstalling) return;

    setInstallingAgents((prev) => ({ ...prev, [name]: true }));
    setInstallResults((prev) => ({ ...prev, [name]: { ok: false, message: '', output: null } }));
    setExpandedOutputs((prev) => ({ ...prev, [name]: false }));

    try {
      const result = await api.installAgent(name);
      const installedPath = typeof result.path === 'string' && result.path ? result.path : null;
      setInstallResults((prev) => ({
        ...prev,
        [name]: { ok: result.ok, message: result.message, output: result.output },
      }));
      if (result.ok) {
        if (installedPath) {
          setAgents((prev) => ({
            ...prev,
            [name]: { ...prev[name], cli_path: installedPath },
          }));
        }
        await detect(name, installedPath || agents[name]?.cli_path || name);
      }
    } catch (e) {
      setInstallResults((prev) => ({
        ...prev,
        [name]: { ok: false, message: String(e), output: null },
      }));
    } finally {
      setInstallingAgents((prev) => ({ ...prev, [name]: false }));
    }
  };

  const toggleOutput = (name: string) => {
    setExpandedOutputs((prev) => ({ ...prev, [name]: !prev[name] }));
  };

  // Gate the wizard's Continue when OpenCode is enabled and ready but hasn't
  // been granted ``permission: "allow"`` yet — without it every tool call
  // silently waits for an approval prompt avibe can't answer, so the
  // user must write it before proceeding. Fails open while the status is still
  // unknown (statusLoaded false) so a transient probe error can't trap the
  // user, and never gates the Settings page (isPage).
  const opencodeAgent = agents['opencode'];
  const opencodeNeedsPermission =
    !isPage &&
    !!opencodeAgent?.enabled &&
    opencodeAgent?.status === 'ok' &&
    permission.statusLoaded &&
    !permission.permissionAllowed;
  const canContinue =
    Object.values(agents).some((agent) => agent.enabled) && !opencodeNeedsPermission;

  const handlePrimaryAction = async () => {
    // Wait for an in-flight provider-modal sync and fold its result into the
    // snapshot we save, so a quick close-then-continue can't persist a stale
    // ``agents`` object that reverts edits made inside the modal.
    let mergedAgents = agents;
    if (syncRef.current) {
      try {
        const synced = await syncRef.current;
        if (synced) {
          // Fold provider edits from the modal into the saved snapshot, but keep
          // ``enabled`` from the live ``agents`` state — a toggle flipped after
          // the sync started must win over the sync's stale snapshot.
          mergedAgents = { ...agents };
          for (const [backendName, syncedAgent] of Object.entries(synced)) {
            mergedAgents[backendName] = {
              ...syncedAgent,
              enabled: agents[backendName]?.enabled ?? syncedAgent.enabled,
            };
          }
        }
      } catch {
        // ignore — fall back to the current snapshot
      }
      syncRef.current = null;
    }
    const nextData = { agents: mergedAgents };
    if (isPage && onSave) {
      await onSave(nextData);
      return;
    }
    onNext(nextData);
  };

  const enabledCount = Object.values(agents).filter((a) => a.enabled && a.status === 'ok').length;

  // Page mode keeps the existing settings shell — render the inner content only
  const Inner = (
    <>
      <div className="flex flex-col gap-3 rounded-xl border border-border bg-background px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
            {t('agentDetection.backendsLabel')}
          </span>
          <p className="mt-0.5 text-[12px] leading-snug text-muted">{t('agentDetection.detectedHelper')}</p>
        </div>
        <Button type="button" variant="secondary" size="sm" onClick={detectAll} className="shrink-0">
          <RefreshCw className="size-3.5" /> {t('agentDetection.rescan')}
        </Button>
      </div>

      <div className="flex flex-col gap-3">
        {Object.entries(agents).map(([name, agent]) => {
          const meta = getBackendUiMeta(name);
          const ready = agent.status === 'ok';
          const description = t(`settings.backends.${name}Description`, { defaultValue: '' });
          // Supported backends can open the provider config regardless of
          // detection status — the modal's runtime card lets the user point at a
          // custom CLI path when auto-detection missed an installed binary.
          const canConfigure = PROVIDER_BACKENDS.has(name);
          return (
            <div
              key={name}
              className="flex flex-col gap-3.5 rounded-xl border border-border bg-background px-5 py-4 transition-colors hover:border-border-strong"
            >
              {/* Top row — identity + status + enable switch. */}
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2">
                    <BackendIcon backend={name as BackendId} size={18} variant="glyph" />
                  </div>
                  <div className="min-w-0">
                    <h3 className="text-[13px] font-semibold text-foreground">{meta.label}</h3>
                    {description && (
                      <p className="mt-0.5 truncate text-[11px] leading-snug text-muted">{description}</p>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <BackendLifecycleChip
                    name={name}
                    enabled={agent.enabled}
                    cliStatus={agent.status || 'unknown'}
                    onChanged={async (info) => {
                      // After a successful (re)install the chip hands back the
                      // path the installer landed at — adopt it before
                      // detecting, otherwise a stale ``agent.cli_path`` from
                      // this render keeps the row in a false ``missing`` state.
                      const installedPath = info?.installedPath || null;
                      if (installedPath) {
                        setAgents((prev) => ({
                          ...prev,
                          [name]: { ...prev[name], cli_path: installedPath },
                        }));
                      }
                      await detect(name, installedPath || agent.cli_path);
                    }}
                  />
                  <ToggleSwitch enabled={agent.enabled} onClick={() => toggle(name, !agent.enabled)} />
                </div>
              </div>

              {/* Action row — configure / set up Allow / install. */}
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-3">
                  {canConfigure &&
                    (isPage ? (
                      meta.settingsRoute && (
                        <Button asChild variant="secondary" size="sm">
                          <Link to={meta.settingsRoute}>
                            <Sliders className="size-3.5" />
                            {t('agentDetection.configureProvider')}
                            <ExternalLink className="size-3.5" />
                          </Link>
                        </Button>
                      )
                    ) : (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() => setProviderModal(name)}
                      >
                        <Sliders className="size-3.5" />
                        {t('agentDetection.configureProvider')}
                      </Button>
                    ))}

                  {name === 'opencode' && (
                    <OpencodePermissionSetup
                      cliReady={ready}
                      permissionAllowed={permission.permissionAllowed}
                      state={permission.state}
                      message={permission.message}
                      onSetup={() => void permission.setupPermission()}
                      className="w-full"
                    />
                  )}

                  {isMissing(agent) && (
                    <>
                      <Button
                        type="button"
                        variant="brand-cyan"
                        size="sm"
                        onClick={() => installAgent(name)}
                        disabled={isAnyInstalling}
                      >
                        {installingAgents[name] ? (
                          <RefreshCw className="size-3.5 animate-spin" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {installingAgents[name]
                          ? t('agentDetection.installing')
                          : t('agentDetection.installAgentNamed', { name: meta.label })}
                      </Button>
                      {installResults[name]?.message && (
                        <span
                          className={clsx('text-[11px]', installResults[name].ok ? 'text-mint' : 'text-danger')}
                        >
                          {installResults[name].message}
                        </span>
                      )}
                    </>
                  )}
                </div>

                {isMissing(agent) && installResults[name]?.output && (
                  <div>
                    <button
                      onClick={() => toggleOutput(name)}
                      className="inline-flex items-center gap-1 text-[11px] text-cyan transition hover:text-cyan/80"
                    >
                      {expandedOutputs[name] ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                      {t('agentDetection.showOutput')}
                    </button>
                    {expandedOutputs[name] && (
                      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded border border-border bg-background px-3 py-2 font-mono text-[11px] text-muted">
                        {installResults[name].output}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Wizard-mode provider config modal — reuses the same component tree as
          the Settings route. Page mode navigates to the route instead (see the
          Link above), so the dialog is wizard-only. */}
      {!isPage && (
        <Dialog
          open={providerModal !== null}
          onOpenChange={(open) => {
            if (!open) {
              const name = providerModal;
              setProviderModal(null);
              // Re-read config + re-detect so runtime edits (enabled / cli_path /
              // provider defaults) and the status pill reflect whatever changed
              // inside the modal. Keep the promise so Continue can await it.
              if (name) syncRef.current = syncBackendFromConfig(name);
              // The Configure-provider modal embeds its OWN useOpencodePermission
              // instance, so a permission write inside it doesn't touch this
              // wizard's gate/callout state. Re-read opencode.json so the gate
              // clears and the callout hides once allow was granted in the modal.
              if (name === 'opencode') void permission.refreshStatus();
            }
          }}
        >
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle>
                {providerModal
                  ? t('agentDetection.configureProviderTitle', { name: getBackendUiMeta(providerModal).label })
                  : ''}
              </DialogTitle>
            </DialogHeader>
            {providerModal && (
              // ``deferRestart``: the wizard runs before/while the controller
              // is started, so the embedded runtime Save persists the cli_path
              // without attempting a (would-be-unacknowledged) restart that
              // otherwise reads as a failed/dirty save.
              <BackendProviderConfig backend={providerModal as RuntimeBackendId} hideEnableToggle deferRestart />
            )}
          </DialogContent>
        </Dialog>
      )}
    </>
  );

  if (isPage) {
    return (
      <div className="flex flex-col gap-4">
        {Inner}
        <div className="flex justify-end">
          <Button variant="brand" size="default" onClick={() => void handlePrimaryAction()} disabled={!canContinue || syncing}>
            {t('common.save')}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex w-full justify-center">
      <WizardCard className="gap-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-2">
            <EyebrowBadge tone="mint">{t('agentDetection.eyebrow')}</EyebrowBadge>
            <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
              {t('agentDetection.title')}
            </h2>
            <p className="max-w-[560px] text-[14px] leading-[1.55] text-muted">
              {t('agentDetection.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-foreground/[0.04] px-3 py-1.5">
            <span className="font-mono text-[11px] font-bold uppercase tracking-[0.16em] text-mint">
              {enabledCount} active
            </span>
          </div>
        </div>

        {Inner}

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          {onBack ? (
            <Button
              type="button"
              variant="secondary"
              size="default"
              onClick={onBack}
              className="font-semibold"
            >
              <ArrowLeft size={14} strokeWidth={2.25} />
              {t('common.back')}
            </Button>
          ) : (
            <span />
          )}
          <div className="flex flex-1 flex-col items-end gap-1.5 sm:flex-none">
            {opencodeNeedsPermission && (
              <p className="text-right text-[12px] text-gold">
                {t('agentDetection.permissionGateHint')}
              </p>
            )}
            <Button
              type="button"
              variant="brand"
              size="default"
              onClick={() => void handlePrimaryAction()}
              disabled={!canContinue || syncing}
              className="w-full sm:w-auto"
            >
              {t('common.continue')}
              <ArrowRight size={14} strokeWidth={2.25} />
            </Button>
          </div>
        </div>
      </WizardCard>
    </div>
  );
};

import React, { useEffect, useState } from 'react';
import { Bot, FolderOpen, HelpCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Input } from '@/components/ui/input';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { AgentRoutePicker } from '../workbench/AgentRoutePicker';
import type { VibeAgentBrief } from '../../context/ApiContext';

// Mirrors design.pen `asPXu` (VR/RoutingConfig). Shared between groups (channels)
// and users — same form, same fields, same styles. The only difference is whether
// the @mention requirement segmented toggle is shown.
//
// The agent/model/effort route is the SAME AgentRoutePicker used by the workbench
// chat header + create surfaces (one source of truth), replacing the former three
// separate selects. Its built-in "+ new agent → /agents" entry covers managing /
// adding agents from wherever a route is chosen.

export interface RoutingConfigValue {
  custom_cwd: string;
  routing: {
    agent_name?: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    opencode_agent?: string | null;
    opencode_model?: string | null;
    opencode_reasoning_effort?: string | null;
    claude_agent?: string | null;
    claude_model?: string | null;
    claude_reasoning_effort?: string | null;
    codex_agent?: string | null;
    codex_model?: string | null;
    codex_reasoning_effort?: string | null;
  };
  show_message_types: string[];
  require_mention?: boolean | null;
  require_bind?: boolean | null;
}

export interface RoutingConfigPanelProps {
  value: RoutingConfigValue;
  onChange: (patch: Partial<RoutingConfigValue>) => void;
  onBrowseDirectory: () => void;
  globalConfig: any;
  /** Show the require-@mention and bound-only on/off switches (channels only). */
  showRequireMention?: boolean;
  /** Platform key used to derive inherited @mention default (e.g., 'slack', 'discord'). */
  inheritsFromKey?: string;
  /** Vibe Agent catalog — passed already-loaded from the parent. */
  vibeAgents?: VibeAgentBrief[];
  defaultAgentName?: string | null;
  availableMessageTypes?: string[];
  // Legacy backend-model props: no longer read here (AgentRoutePicker self-loads
  // models + effort options). Kept on the interface so existing callers still
  // type-check; a follow-up can drop them and the parents' model preloading.
  opencodeOptions?: any;
  claudeAgents?: { id: string; name: string }[];
  claudeModels?: string[];
  claudeModelLabels?: Record<string, string>;
  claudeReasoningOptions?: Record<string, { value: string; label: string }[]>;
  codexAgents?: { id: string; name: string }[];
  codexModels?: string[];
  /** Custom footer slot — e.g., admin/remove actions on the users page. */
  footerActions?: React.ReactNode;
  /** Wrapper class — controls outer padding/border. Default: 'border-t border-border/60 px-5 py-4'. */
  containerClass?: string;
}

/** Input that only commits value on blur */
function BlurInput({
  value,
  onCommit,
  ...props
}: { value: string; onCommit: (v: string) => void } & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'onBlur'>) {
  const [local, setLocal] = useState(value);
  useEffect(() => setLocal(value), [value]);
  return (
    <Input
      {...props}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => { if (local !== value) onCommit(local); }}
    />
  );
}

export const RoutingConfigPanel: React.FC<RoutingConfigPanelProps> = ({
  value,
  onChange,
  onBrowseDirectory,
  globalConfig,
  showRequireMention = true,
  inheritsFromKey,
  vibeAgents = [],
  defaultAgentName,
  availableMessageTypes = ['assistant', 'toolcall'],
  footerActions,
  containerClass = 'border-t border-border/60 px-5 py-4',
}) => {
  const { t } = useTranslation();

  const selectedVibeAgent = vibeAgents.find((agent) => agent.name === value.routing.agent_name) || null;
  const defaultVibeAgent = vibeAgents.find((agent) => agent.name === defaultAgentName) || null;

  // Picking an agent re-seeds model/effort, so always clear the deprecated
  // per-backend overrides on any route change (kept for back-compat on read).
  const clearLegacyOverrides = (routing: RoutingConfigValue['routing']) => ({
    ...routing,
    opencode_model: null,
    opencode_reasoning_effort: null,
    claude_model: null,
    claude_reasoning_effort: null,
    codex_model: null,
    codex_reasoning_effort: null,
  });

  // Access row: working dir (+ optional require_mention / require_bind for
  // channels). The Agent route picker lives in the row below.
  const accessGridCols = showRequireMention ? 'md:grid-cols-3' : 'md:grid-cols-1';

  return (
    <div className={clsx('space-y-4', containerClass)}>
      <div className={clsx('grid grid-cols-1 gap-3', accessGridCols)}>
        {/* Working directory. On the users page (no require-mention column) it
            would otherwise stretch the full row, so cap it at ~40%. */}
        <div className={clsx('space-y-1', !showRequireMention && 'md:max-w-[40%]')}>
          <label className="text-xs font-medium uppercase text-muted">{t('channelList.workingDirectory')}</label>
          <div className="flex gap-1.5">
            <BlurInput
              type="text"
              placeholder={globalConfig?.runtime?.default_cwd || t('channelList.useGlobalDefault')}
              value={value.custom_cwd}
              onCommit={(v) => onChange({ custom_cwd: v })}
              className="flex-1 font-mono text-[12px]"
            />
            <button
              type="button"
              onClick={onBrowseDirectory}
              title={t('directoryBrowser.title')}
              className="shrink-0 rounded-lg border border-border bg-surface-3/60 px-2 py-2 text-muted transition-colors hover:border-cyan/40 hover:bg-surface-2/70 hover:text-foreground"
            >
              <FolderOpen size={14} />
            </button>
          </div>
        </div>

        {/* Require @mention (channels only). Simple on/off switch. When the
            per-group value is unset (null), the switch reflects the platform
            group default; toggling writes an explicit boolean for this group. */}
        {showRequireMention && (() => {
          const effective =
            value.require_mention === null || value.require_mention === undefined
              ? !!(globalConfig as any)?.[inheritsFromKey || '']?.require_mention
              : value.require_mention;
          return (
            <div className="space-y-1">
              <label className="text-xs font-medium uppercase text-muted">{t('channelList.requireMention')}</label>
              <div className="flex h-9 items-center">
                <ToggleSwitch
                  enabled={effective}
                  onClick={() => onChange({ require_mention: !effective })}
                />
              </div>
            </div>
          );
        })()}

        {/* Require bind (channels only): when On, only bound users can drive the
            agent here; unbound members are silently ignored. Unlike @mention,
            this does NOT inherit the platform default at runtime — the default
            only seeds newly-enabled groups (copy-on-enable), so an unset value
            means "anyone" and the switch shows off. Toggling writes an explicit
            boolean for this group. */}
        {showRequireMention && (() => {
          const effective = value.require_bind === true;
          return (
            <div className="space-y-1">
              <label className="text-xs font-medium uppercase text-muted">{t('channelList.requireBind')}</label>
              <div className="flex h-9 items-center">
                <ToggleSwitch
                  enabled={effective}
                  onClick={() => onChange({ require_bind: !effective })}
                />
              </div>
            </div>
          );
        })()}
      </div>

      {/* Routing: one unified Agent picker (agent → model → effort), shared with
          the workbench chat/create surfaces. Empty = inherit the global default. */}
      <div className="space-y-1">
        <label className="flex items-center gap-1.5 text-xs font-medium uppercase text-muted">
          <Bot size={12} className="text-muted" />
          {t('channelList.agent')}
        </label>
        <AgentRoutePicker
          value={{
            // Resolve the DISPLAYED route. An explicit agent with an empty
            // model/effort means "inherit that agent's", so fall back to the
            // selected agent's own model/effort — otherwise the effort column
            // resolves options against the backend defaults instead of the
            // inherited model (e.g. a Claude agent's xhigh/max would vanish).
            // agent_backend also falls back to the default agent so the columns
            // resolve while inheriting the global default.
            agent_backend: selectedVibeAgent?.backend ?? defaultVibeAgent?.backend ?? null,
            agent_name: value.routing.agent_name ?? null,
            model: value.routing.model ?? selectedVibeAgent?.model ?? null,
            reasoning_effort: value.routing.reasoning_effort ?? selectedVibeAgent?.reasoning_effort ?? null,
          }}
          agents={vibeAgents}
          onChange={(patch) => {
            // Concrete routes, matching the workbench picker + the other call
            // sites: merge only the keys PRESENT in the (partial) patch. A
            // present field wins (incl. an explicit null that clears it); an
            // absent field keeps its stored value. We deliberately do NOT
            // special-case agent picks to null model/effort — that discarded the
            // first model pick on an inherited-default route, where the pick
            // arrives as the materialized agent_name + the chosen model together.
            const next = { ...value.routing };
            if ('agent_name' in patch) next.agent_name = patch.agent_name ?? null;
            if ('model' in patch) next.model = patch.model ?? null;
            if ('reasoning_effort' in patch) next.reasoning_effort = patch.reasoning_effort ?? null;
            onChange({ routing: clearLegacyOverrides(next) });
          }}
          defaultLabel={
            defaultVibeAgent ? `${t('common.default')} · ${defaultVibeAgent.name}` : t('common.default')
          }
          defaultRoute={
            defaultVibeAgent
              ? {
                  agent_backend: defaultVibeAgent.backend,
                  agent_name: defaultVibeAgent.name,
                  model: defaultVibeAgent.model,
                  reasoning_effort: defaultVibeAgent.reasoning_effort,
                }
              : undefined
          }
          // Only a FULLY-inheriting scope (no overrides at all) displays the
          // default route. A scope that inherits the agent but overrides
          // model/effort must show its own values, not the default agent's,
          // otherwise the override is masked and silently overwritten.
          isDefaultRoute={
            !value.routing.agent_name && !value.routing.model && !value.routing.reasoning_effort
          }
          align="start"
          triggerClassName="w-full"
        />
      </div>

      {/* Show message types chips */}
      <div className="space-y-2">
        <div className="flex items-center gap-1 text-xs font-medium uppercase text-muted">
          {t('channelList.showMessageTypes')}
          <span className="group relative">
            <HelpCircle size={12} className="cursor-help text-muted/50" />
            <span className="pointer-events-none absolute bottom-full left-0 z-10 mb-2 w-64 whitespace-normal rounded bg-text px-3 py-2 text-xs font-normal normal-case text-bg opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
              {t('channelList.showMessageTypesHint')}
            </span>
          </span>
        </div>
        <div className="flex flex-wrap gap-2 text-sm">
          {availableMessageTypes.map((msgType) => {
            const checked = (value.show_message_types || []).includes(msgType);
            const label = t(`channelList.messageType.${msgType}`);
            return (
              <button
                key={msgType}
                type="button"
                aria-pressed={checked}
                onClick={() => {
                  const next = checked
                    ? (value.show_message_types || []).filter((v) => v !== msgType)
                    : [...(value.show_message_types || []), msgType];
                  onChange({ show_message_types: next });
                }}
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium transition-colors',
                  checked
                    ? 'border-mint/40 bg-mint/15 text-mint'
                    : 'border-border bg-foreground/[0.02] text-muted hover:border-border-strong hover:text-foreground'
                )}
              >
                <span
                  className={clsx(
                    'size-1.5 rounded-full',
                    checked ? 'bg-mint shadow-[0_0_6px_rgba(91,255,160,0.7)]' : 'bg-muted/50'
                  )}
                />
                {label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Custom footer slot — e.g., admin/remove buttons on /users */}
      {footerActions && (
        <div className="flex items-center justify-end gap-2 border-t border-border/60 pt-3">
          {footerActions}
        </div>
      )}
    </div>
  );
};

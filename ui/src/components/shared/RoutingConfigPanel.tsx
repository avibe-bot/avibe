import React, { useEffect, useState } from 'react';
import { Bot, FolderOpen, HelpCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Combobox } from '../ui/combobox';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { BackendIcon } from '../visual';
import { CompactSelect } from '../settings/SettingsPrimitives';
import { modelOptionLabel } from '../../lib/backendModels';

// Mirrors design.pen `asPXu` (VR/RoutingConfig). Shared between groups (channels)
// and users — same form, same fields, same styles. The only difference is whether
// the @mention requirement segmented toggle is shown.

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
  /** Show the require-@mention segmented control (Inherit / On / Off). */
  showRequireMention?: boolean;
  /** Platform key used to derive inherited @mention default (e.g., 'slack', 'discord'). */
  inheritsFromKey?: string;
  /** Backend lookup data — pass already-loaded values from the parent. */
  vibeAgents?: {
    name: string;
    description?: string | null;
    backend: string;
    model?: string | null;
    reasoning_effort?: string | null;
  }[];
  defaultAgentName?: string | null;
  opencodeOptions?: any;
  claudeAgents?: { id: string; name: string }[];
  claudeModels?: string[];
  claudeModelLabels?: Record<string, string>;
  claudeReasoningOptions?: Record<string, { value: string; label: string }[]>;
  codexAgents?: { id: string; name: string }[];
  codexModels?: string[];
  /** Optional footer slot — e.g., admin/remove actions on the users page. */
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
  opencodeOptions,
  claudeModels = [],
  claudeModelLabels = {},
  claudeReasoningOptions = {},
  codexModels = [],
  footerActions,
  containerClass = 'border-t border-border/60 px-5 py-4',
}) => {
  const { t } = useTranslation();

  const selectedVibeAgent = vibeAgents.find((agent) => agent.name === value.routing.agent_name) || null;
  const defaultVibeAgent = vibeAgents.find((agent) => agent.name === defaultAgentName) || null;
  const inheritedVibeAgent = selectedVibeAgent || defaultVibeAgent;
  const effectiveBackend = selectedVibeAgent?.backend || defaultVibeAgent?.backend || 'opencode';
  const effectiveModel = value.routing.model || inheritedVibeAgent?.model || '';

  const getOpenCodeReasoningOptions = (modelKey: string) => {
    const lookup = opencodeOptions?.reasoning_options || {};
    if (lookup && typeof lookup === 'object') {
      return (lookup as Record<string, { value: string; label: string }[]>)[modelKey] || [];
    }
    return [];
  };

  const getClaudeReasoning = (model: string) => {
    const modelKey = model || '';
    const cached = claudeReasoningOptions[modelKey];
    if (cached?.length) return cached;
    const fallback = claudeReasoningOptions[''] || [];
    const normalized = modelKey.toLowerCase();
    if (normalized.includes('claude-opus-4-7') || normalized.includes('claude-fable-5') || normalized === 'opus' || normalized === 'opus[1m]') {
      const opts = [...fallback];
      if (!opts.some((o) => o.value === 'xhigh')) opts.push({ value: 'xhigh', label: 'Extra High' });
      if (!opts.some((o) => o.value === 'max')) opts.push({ value: 'max', label: 'Max' });
      return opts;
    }
    if (normalized.includes('claude-opus-4-6') || normalized.includes('claude-sonnet-4-6')) {
      return fallback.some((o) => o.value === 'max') ? fallback : [...fallback, { value: 'max', label: 'Max' }];
    }
    return fallback;
  };

  const getReasoningLabel = (val: string, fallback: string) => {
    switch (val) {
      case 'low': return t('channelList.reasoningLow');
      case 'medium': return t('channelList.reasoningMedium');
      case 'high': return t('channelList.reasoningHigh');
      case 'xhigh': return t('channelList.reasoningXHigh');
      case 'max': return t('channelList.reasoningMax');
      default: return fallback;
    }
  };

  const clearLegacyOverrides = (routing: RoutingConfigValue['routing']) => ({
    ...routing,
    opencode_model: null,
    opencode_reasoning_effort: null,
    claude_model: null,
    claude_reasoning_effort: null,
    codex_model: null,
    codex_reasoning_effort: null,
  });

  const buildModelRoutingPatch = (model: string | null) => {
    return clearLegacyOverrides({
      ...value.routing,
      model,
      reasoning_effort: null,
    });
  };

  const buildReasoningRoutingPatch = (reasoningEffort: string | null) => {
    return clearLegacyOverrides({
      ...value.routing,
      reasoning_effort: reasoningEffort,
    });
  };

  const modelOverrideControl = (() => {
    if (effectiveBackend === 'opencode') {
      return (
        <CompactSelect
          value={value.routing.model || ''}
          onChange={(e) => onChange({
            routing: buildModelRoutingPatch(e.target.value || null),
          })}
          className="w-full"
        >
          <option value="">{t('common.default')}</option>
          {(opencodeOptions?.models?.providers || []).flatMap((provider: any) => {
            const pid = provider.id || provider.provider_id || provider.name;
            const pLabel = provider.name || pid;
            const models = provider.models || {};
            if (Array.isArray(models)) {
              return models.map((m: any) => {
                const mid = typeof m === 'string' ? m : m.id;
                return <option key={`${pid}:${mid}`} value={`${pid}/${mid}`}>{pLabel}/{mid}</option>;
              });
            }
            return Object.keys(models).map((mid) => (
              <option key={`${pid}:${mid}`} value={`${pid}/${mid}`}>{pLabel}/{mid}</option>
            ));
          })}
        </CompactSelect>
      );
    }
    const models = effectiveBackend === 'claude' ? claudeModels : codexModels;
    const placeholder = effectiveBackend === 'claude'
      ? t('channelList.claudeModelPlaceholder')
      : t('channelList.codexModelPlaceholder');
    return (
      <Combobox
        options={[
          { value: '', label: t('common.default') },
          ...models.map(m => ({
            value: m,
            label: effectiveBackend === 'claude' ? modelOptionLabel(m, claudeModelLabels) : m,
          })),
        ]}
        value={value.routing.model || ''}
        onValueChange={(v) => onChange({
          routing: buildModelRoutingPatch(v || null),
        })}
        placeholder={placeholder}
        searchPlaceholder={t('channelList.searchModel')}
        allowCustomValue={true}
      />
    );
  })();

  const reasoningOptions = (() => {
    if (effectiveBackend === 'claude') {
      return getClaudeReasoning(effectiveModel)
        .filter((option) => option.value !== '__default__')
        .map((option) => ({ value: option.value, label: getReasoningLabel(option.value, option.label) }));
    }
    if (effectiveBackend === 'opencode') {
      const options = getOpenCodeReasoningOptions(effectiveModel);
      if (options.length) return options;
    }
    return ['low', 'medium', 'high', 'xhigh'].map((value) => ({ value, label: getReasoningLabel(value, value) }));
  })();

  // Access row: working dir (+ optional require_mention / require_bind for
  // channels). The Agent selector lives in the routing row below so it shares a
  // line with Model + Reasoning effort.
  const accessGridCols = showRequireMention ? 'md:grid-cols-3' : 'md:grid-cols-1';

  return (
    <div className={clsx('space-y-4', containerClass)}>
      <div className={clsx('grid grid-cols-1 gap-3', accessGridCols)}>
        {/* Working directory */}
        <div className="space-y-1">
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

        {/* Require @mention (channels only) */}
        {showRequireMention && (() => {
          const current: 'inherit' | 'on' | 'off' =
            value.require_mention === null || value.require_mention === undefined
              ? 'inherit'
              : value.require_mention
                ? 'on'
                : 'off';
          const setMention = (next: 'inherit' | 'on' | 'off') => {
            onChange({ require_mention: next === 'inherit' ? null : next === 'on' });
          };
          const inheritedOn = !!(globalConfig as any)?.[inheritsFromKey || '']?.require_mention;
          const segs: { id: 'inherit' | 'on' | 'off'; label: string }[] = [
            {
              id: 'inherit',
              label: `${t('common.inherit')} (${
                inheritedOn ? t('channelList.mentionStatusOn') : t('channelList.mentionStatusOff')
              })`,
            },
            { id: 'on', label: t('channelList.requireMentionOn') },
            { id: 'off', label: t('channelList.requireMentionOff') },
          ];
          return (
            <div className="space-y-1">
              <label className="text-xs font-medium uppercase text-muted">{t('channelList.requireMention')}</label>
              <div
                role="radiogroup"
                aria-label={t('channelList.requireMention') as string}
                className="flex h-9 items-stretch gap-0.5 rounded-md border border-border bg-foreground/[0.03] p-0.5"
              >
                {segs.map((seg) => {
                  const active = current === seg.id;
                  return (
                    <button
                      key={seg.id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => setMention(seg.id)}
                      className={clsx(
                        // The "inherit" label carries the inherited state in
                        // parentheses, so it needs more room than on/off to
                        // avoid wrapping.
                        'whitespace-nowrap rounded-[4px] px-2 text-[12px] transition-colors',
                        seg.id === 'inherit' ? 'flex-[1.6]' : 'flex-1',
                        active
                          ? 'border border-mint/30 bg-mint-soft font-bold text-mint'
                          : 'font-medium text-muted hover:text-foreground'
                      )}
                    >
                      {seg.label}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })()}

        {/* Require bind (channels only): when On, only bound users can drive the
            agent here; unbound members are silently ignored. */}
        {showRequireMention && (() => {
          const on = value.require_bind === true;
          const setBind = (next: boolean) => {
            onChange({ require_bind: next ? true : null });
          };
          const segs: { id: 'off' | 'on'; label: string }[] = [
            { id: 'off', label: t('channelList.requireBindOff') },
            { id: 'on', label: t('channelList.requireBindOn') },
          ];
          return (
            <div className="space-y-1">
              <label className="text-xs font-medium uppercase text-muted">{t('channelList.requireBind')}</label>
              <div
                role="radiogroup"
                aria-label={t('channelList.requireBind') as string}
                className="flex h-9 items-stretch gap-0.5 rounded-md border border-border bg-foreground/[0.03] p-0.5"
              >
                {segs.map((seg) => {
                  const active = (seg.id === 'on') === on;
                  return (
                    <Button
                      key={seg.id}
                      type="button"
                      variant="ghost"
                      size="sm"
                      role="radio"
                      aria-checked={active}
                      onClick={() => setBind(seg.id === 'on')}
                      className={clsx(
                        'h-auto flex-1 rounded-[4px] px-2.5 text-[12px] shadow-none focus-visible:ring-1',
                        active
                          ? 'border border-mint/30 bg-mint-soft font-bold text-mint'
                          : 'font-medium text-muted hover:text-foreground'
                      )}
                    >
                      {seg.label}
                    </Button>
                  );
                })}
              </div>
            </div>
          );
        })()}
      </div>

      {/* Routing: Agent + Model + Reasoning effort share one row */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        {/* Vibe Agent */}
        <div className="space-y-1">
          <label className="text-xs font-medium uppercase text-muted">{t('channelList.agent')}</label>
          <div className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2">
              <Bot size={14} className="text-muted" />
            </span>
            <CompactSelect
              value={value.routing.agent_name || ''}
              onChange={(e) => {
                const nextName = e.target.value || null;
                onChange({
                  routing: {
                    ...value.routing,
                    agent_name: nextName,
                    model: null,
                    reasoning_effort: null,
                    opencode_model: null,
                    opencode_reasoning_effort: null,
                    claude_model: null,
                    claude_reasoning_effort: null,
                    codex_model: null,
                    codex_reasoning_effort: null,
                  },
                });
              }}
              className="w-full pl-9 pr-3"
            >
              <option value="">
                {defaultVibeAgent ? `${t('common.default')} · ${defaultVibeAgent.name}` : t('common.default')}
              </option>
              {vibeAgents.map((agent) => (
                <option key={agent.name} value={agent.name}>
                  {agent.name}
                  {agent.backend ? ` · ${agent.backend}` : ''}
                  {agent.model ? ` / ${agent.model}` : ''}
                </option>
              ))}
            </CompactSelect>
          </div>
          {inheritedVibeAgent && (
            <div className="flex items-center gap-1.5 text-[11px] text-muted">
              <BackendIcon backend={inheritedVibeAgent.backend} variant="glyph" size={12} />
              <span className="truncate">
                {inheritedVibeAgent.backend}
                {inheritedVibeAgent.model ? ` / ${inheritedVibeAgent.model}` : ''}
                {inheritedVibeAgent.reasoning_effort ? ` / ${inheritedVibeAgent.reasoning_effort}` : ''}
              </span>
            </div>
          )}
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium uppercase text-muted">{t('channelList.model')}</label>
          {modelOverrideControl}
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium uppercase text-muted">{t('channelList.reasoningEffort')}</label>
          <CompactSelect
            value={value.routing.reasoning_effort || ''}
            onChange={(e) => onChange({
              routing: buildReasoningRoutingPatch(e.target.value || null),
            })}
            className="w-full"
          >
            <option value="">{t('common.default')}</option>
            {reasoningOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </CompactSelect>
        </div>
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
          {['assistant', 'toolcall'].map((msgType) => {
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

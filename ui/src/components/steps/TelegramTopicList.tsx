import React from 'react';
import { ChevronDown, ChevronUp, GitBranch, RotateCcw, SlidersHorizontal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Button } from '../ui/button';
import { ToggleSwitch } from '../settings/SettingsPrimitives';

export interface TelegramTopic {
  id: string;
  name?: string;
  configured?: boolean;
  last_seen_at?: string;
}

interface TelegramTopicListProps<T extends { enabled: boolean }> {
  topics: TelegramTopic[];
  configs: Record<string, T>;
  expandedTopicId: string | null;
  onToggleExpanded: (topicId: string) => void;
  onCustomize: (topicId: string) => void;
  onReset: (topicId: string) => void;
  onSetEnabled: (topicId: string, enabled: boolean) => void;
  renderEditor: (topicId: string, value: T) => React.ReactNode;
}

export function TelegramTopicList<T extends { enabled: boolean }>({
  topics,
  configs,
  expandedTopicId,
  onToggleExpanded,
  onCustomize,
  onReset,
  onSetEnabled,
  renderEditor,
}: TelegramTopicListProps<T>) {
  const { t } = useTranslation();

  if (topics.length === 0) {
    return (
      <div className="border-t border-border px-5 py-4">
        <div className="rounded-lg border border-dashed border-border bg-background/60 px-4 py-5 text-center">
          <div className="text-sm font-medium text-foreground">{t('channelList.topicEmptyTitle')}</div>
          <div className="mt-1 text-xs text-muted">{t('channelList.topicEmptyDesc')}</div>
        </div>
      </div>
    );
  }

  return (
    <section className="border-t border-border bg-background/35 px-5 py-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <GitBranch size={15} className="text-cyan" />
            {t('channelList.topicSettingsTitle')}
            <span className="rounded-full border border-border bg-foreground/[0.04] px-2 py-0.5 text-[10px] font-medium text-muted">
              {topics.length}
            </span>
          </div>
          <p className="mt-1 text-xs text-muted">{t('channelList.topicSettingsDesc')}</p>
        </div>
      </div>

      <div className="space-y-2">
        {topics.map((topic) => {
          const value = configs[topic.id];
          const customized = Boolean(value);
          const expanded = expandedTopicId === topic.id;
          return (
            <div
              key={topic.id}
              className={clsx(
                'overflow-hidden rounded-lg border transition-colors',
                customized ? 'border-cyan/30 bg-cyan-soft/10' : 'border-border bg-surface-2/45',
              )}
            >
              <div className="flex items-center gap-3 px-3.5 py-3">
                <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-cyan/25 bg-cyan-soft/20 text-cyan">
                  <GitBranch size={14} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-foreground">
                    {topic.name ||
                      (topic.id === '1'
                        ? t('channelList.topicGeneralName')
                        : t('channelList.topicFallbackName', { id: topic.id }))}
                  </span>
                  <span className="block truncate font-mono text-[10px] text-muted">
                    {t('channelList.topicIdLabel', { id: topic.id })}
                  </span>
                </span>
                <span
                  className={clsx(
                    'rounded-full border px-2 py-0.5 text-[10px] font-medium',
                    customized
                      ? 'border-cyan/30 bg-cyan-soft/25 text-cyan'
                      : 'border-border bg-foreground/[0.04] text-muted',
                  )}
                >
                  {customized ? t('channelList.topicCustomBadge') : t('channelList.topicInheritedBadge')}
                </span>
                {customized ? (
                  <>
                    <span className="inline-flex items-center gap-2 text-[11px] text-muted">
                      {value.enabled ? t('common.enabled') : t('common.disabled')}
                      <ToggleSwitch
                        enabled={value.enabled}
                        onClick={() => onSetEnabled(topic.id, !value.enabled)}
                      />
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={() => onReset(topic.id)}
                      title={t('channelList.topicReset') as string}
                    >
                      <RotateCcw size={13} />
                      {t('channelList.topicReset')}
                    </Button>
                    <button
                      type="button"
                      onClick={() => onToggleExpanded(topic.id)}
                      className="rounded-md p-1.5 text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground"
                      aria-label={expanded ? t('channelList.topicCollapse') : t('channelList.topicExpand')}
                    >
                      {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                    </button>
                  </>
                ) : (
                  <Button type="button" variant="secondary" size="xs" onClick={() => onCustomize(topic.id)}>
                    <SlidersHorizontal size={13} />
                    {t('channelList.topicCustomize')}
                  </Button>
                )}
              </div>
              {customized && expanded && renderEditor(topic.id, value)}
            </div>
          );
        })}
      </div>
    </section>
  );
}

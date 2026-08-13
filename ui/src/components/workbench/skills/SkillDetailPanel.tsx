import type { ReactNode } from 'react';
import { ArrowUp, Bot, Loader2, Trash2, WandSparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { SkillBrief, SkillCheckItem } from '../../../context/ApiContext';
import { BACKEND_LABEL, BACKEND_ORDER, BACKEND_TEXT, backendsFromAgents, type Backend } from '../../../lib/backendAccent';
import { Button } from '../../ui/button';
import { Switch } from '../../ui/switch';

export interface SkillDetailPanelProps {
  skill: SkillBrief;
  projectName?: string;
  /** Backend currently mid add/remove, to show a spinner on that row. */
  busyBackend?: Backend | null;
  /** Update status from `askill check`; drives the "Update" affordance. */
  check?: SkillCheckItem | null;
  updating?: boolean;
  onClose: () => void;
  onToggleBackend: (backend: Backend, next: boolean) => void;
  onUpdate?: () => void;
  onRemove: () => void;
}

function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{children}</span>;
}

/**
 * Right-hand detail panel for a selected installed skill. The "Available to"
 * switches are the core management surface: ON links the skill to that
 * backend (askill add <path> -a <agent>), OFF unlinks it (askill remove -a).
 * Registry-only data (AI quality, commands) isn't in askill `list --json`
 * yet (askill#11), so it's intentionally absent for installed skills.
 */
export function SkillDetailPanel({
  skill,
  projectName,
  busyBackend,
  check,
  updating,
  onClose,
  onToggleBackend,
  onUpdate,
  onRemove,
}: SkillDetailPanelProps) {
  const { t } = useTranslation();
  const linked = new Set(backendsFromAgents(skill.agents));
  const updateAvailable = check?.status === 'update_available';
  const versionDelta =
    check?.localVersion || check?.remoteVersion ? `${check?.localVersion ?? '?'} → ${check?.remoteVersion ?? '?'}` : undefined;
  return (
    <div className="flex flex-col gap-3.5 self-start rounded-2xl border border-border-strong bg-surface p-5">
      <div className="flex items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-[10px] border border-mint/40 bg-mint-soft text-mint shadow-[0_0_18px_-6px_rgba(91,255,160,0.5)]">
          <WandSparkles className="size-5" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-2">
            <span className="truncate text-[16px] font-bold text-foreground">{skill.name}</span>
            {updateAvailable ? (
              <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-gold/40 bg-gold/[0.12] px-1.5 font-mono text-[9px] font-bold uppercase text-gold">
                <ArrowUp className="size-2.5" />
                {t('skills.updateBadge')}
              </span>
            ) : null}
          </div>
          <div className="truncate text-[10.5px] text-muted">
            {skill.scope === 'global'
              ? t('skills.detail.subtitleGlobal')
              : t('skills.detail.subtitleProject', { project: projectName ?? '' })}
          </div>
        </div>
        <Button type="button" variant="ghost" size="icon" className="size-6" onClick={onClose} aria-label={t('common.close')}>
          <X className="size-3.5" />
        </Button>
      </div>

      <div className="flex flex-col gap-1 rounded-[10px] border border-border bg-surface-3 px-3 py-2.5">
        <Eyebrow>{t('skills.detail.source')}</Eyebrow>
        <div className="truncate font-mono text-[11px] text-foreground" title={skill.path}>
          {skill.path}
        </div>
      </div>

      {skill.description ? <p className="text-[12px] leading-relaxed text-muted">{skill.description}</p> : null}

      <div className="flex flex-col gap-2">
        <Eyebrow>{t('skills.detail.availableTo')}</Eyebrow>
        <div className="flex flex-col gap-1.5">
          {BACKEND_ORDER.map((backend) => {
            const on = linked.has(backend);
            const busy = busyBackend === backend;
            return (
              <div key={backend} className="flex items-center gap-2 rounded-lg border border-border bg-surface-3 px-3 py-2">
                <Bot className={clsx('size-3.5', BACKEND_TEXT[backend])} />
                <span className="flex-1 text-[12.5px] font-medium text-foreground">{BACKEND_LABEL[backend]}</span>
                {busy ? (
                  <Loader2 className="size-3.5 animate-spin text-muted" />
                ) : (
                  <Switch checked={on} onCheckedChange={(next) => onToggleBackend(backend, next)} label={BACKEND_LABEL[backend]} />
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-2 pt-1">
        {updateAvailable ? (
          <Button type="button" variant="brand-gold" size="xs" onClick={onUpdate} disabled={updating} title={versionDelta}>
            {updating ? <Loader2 className="size-3 animate-spin" /> : <ArrowUp className="size-3" />}
            {t('skills.detail.update')}
          </Button>
        ) : null}
        <div className="flex-1" />
        <Button type="button" variant="destructive-soft" size="xs" onClick={onRemove}>
          <Trash2 className="size-3" />
          {t('skills.detail.remove')}
        </Button>
      </div>
    </div>
  );
}

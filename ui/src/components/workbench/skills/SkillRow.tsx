import { ArrowUp, Ellipsis, Github, Globe, WandSparkles } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { SkillBrief } from '../../../context/ApiContext';

export interface SkillRowProps {
  skill: SkillBrief;
  selected?: boolean;
  /** Dim + tag rows that are inherited from global into a project view. */
  inherited?: boolean;
  /** Gold "UPDATE" badge when `askill check` flags a newer version. */
  updateAvailable?: boolean;
  onSelect?: () => void;
}

/** One installed-skill row: lead icon · name + description + source/version. */
export function SkillRow({ skill, selected, inherited, updateAvailable, onSelect }: SkillRowProps) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        'group flex w-full items-center gap-3 rounded-lg border px-[14px] py-3 text-left transition',
        selected
          ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
        inherited && 'opacity-[0.66]',
      )}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-[9px] border border-border bg-surface-3">
        <WandSparkles className={clsx('size-4', selected ? 'text-mint-ink' : 'text-muted')} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex items-center gap-2">
          <span className="truncate text-[14px] font-semibold text-foreground">{skill.name}</span>
          {updateAvailable ? (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-gold/40 bg-gold/[0.12] px-1.5 font-mono text-[9px] font-bold uppercase text-gold-ink">
              <ArrowUp className="size-2.5" />
              {t('skills.updateBadge')}
            </span>
          ) : null}
          {inherited ? (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border-strong px-1.5 font-mono text-[9px] font-bold uppercase text-muted">
              <Globe className="size-2.5" />
              {t('skills.globalBadge')}
            </span>
          ) : null}
        </span>
        {skill.description ? <span className="truncate text-[11.5px] text-muted">{skill.description}</span> : null}
        {skill.installSource || skill.version ? (
          <span className="flex items-center gap-1.5 font-mono text-[10px] text-muted">
            {skill.installSource ? (
              <>
                <Github className="size-2.5 shrink-0" />
                <span className="truncate">{skill.installSource}</span>
              </>
            ) : null}
            {skill.installSource && skill.version ? <span>·</span> : null}
            {skill.version ? <span>v{skill.version}</span> : null}
          </span>
        ) : null}
      </span>
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted opacity-0 transition group-hover:opacity-100">
        <Ellipsis className="size-3.5" />
      </span>
    </button>
  );
}

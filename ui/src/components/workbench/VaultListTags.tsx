import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Puzzle, Tag, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { VaultSecret } from '../../context/ApiContext';
import { cn } from '../../lib/utils';
import { isSkillTag, partitionTags, skillName, toSkillTag } from '../../lib/vaultTags';
import { Badge } from '../ui/badge';
import { badgeVariants } from '../ui/badge-variants';
import { Button } from '../ui/button';
import { Checkbox } from '../ui/checkbox';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';

export function VaultFilterTag({ tag, active, onToggle, removable = false }: {
  tag: string;
  active: boolean;
  onToggle: (tag: string) => void;
  removable?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      aria-pressed={active}
      aria-label={removable ? t('vaults.filter.remove', { name: skillName(tag) }) : undefined}
      onClick={() => onToggle(tag)}
      className={cn(
        badgeVariants({ variant: 'outline' }),
        'min-w-0 max-w-full gap-1 transition-colors hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring',
        isSkillTag(tag) ? 'border-violet/40 bg-violet-soft text-violet-ink' : 'text-muted',
        active && 'border-accent bg-accent/10 text-accent-ink',
      )}
    >
      {isSkillTag(tag) ? <Puzzle className="size-3 shrink-0" /> : null}
      <span className="truncate" title={skillName(tag)}>{skillName(tag)}</span>
      {removable ? <X className="size-3 shrink-0" /> : null}
    </button>
  );
}

const orderedTags = (secret: VaultSecret) => {
  const { skills, tags } = partitionTags(secret.tags);
  return [...skills.map(toSkillTag), ...tags];
};

/** Inventory counts stay stable while intersection filters narrow the list. */
export function VaultTagDropdown({ secrets, selected, onToggle, onClear }: {
  secrets: VaultSecret[];
  selected: string[];
  onToggle: (tag: string) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const counts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const secret of secrets) {
      for (const tag of orderedTags(secret)) counts.set(tag, (counts.get(tag) ?? 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1] || skillName(a[0]).localeCompare(skillName(b[0])));
  }, [secrets]);
  const matches = counts.filter(([tag]) => skillName(tag).toLowerCase().includes(query.trim().toLowerCase()));
  return (
    <Popover onOpenChange={(open) => { if (!open) setQuery(''); }}>
      <PopoverTrigger asChild>
        <Button variant="outline" className="h-9" aria-label={selected.length ? t('vaults.filter.selectedTags', { count: selected.length }) : t('vaults.filter.tags')}>
          <Tag className="size-4" />
          {t('vaults.filter.tags')}
          {selected.length > 0 ? <Badge variant="secondary">{selected.length}</Badge> : null}
          <ChevronDown className="size-3.5 text-muted" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="max-w-[calc(100vw-2rem)] p-0" aria-label={t('vaults.filter.tags')}>
        <div className="border-b border-border p-2">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label={t('vaults.filter.searchTags')}
            placeholder={t('vaults.filter.searchTagsPlaceholder', { count: counts.length })}
            className="h-8"
          />
        </div>
        <div className="max-h-72 overflow-y-auto p-1">
          {[true, false].map((skills) => {
            const entries = matches.filter(([tag]) => isSkillTag(tag) === skills);
            return entries.length ? (
              <fieldset key={String(skills)} className="min-w-0 py-1">
                <legend className="px-2 py-1 text-xs font-medium text-muted">
                  {t(skills ? 'vaults.filter.skills' : 'vaults.filter.tagsByUsage')}
                </legend>
                {entries.map(([tag, count]) => (
                  <button
                    key={tag}
                    type="button"
                    role="checkbox"
                    aria-checked={selected.includes(tag)}
                    aria-label={t('vaults.filter.entry', { name: skillName(tag), count })}
                    onClick={() => onToggle(tag)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Checkbox checked={selected.includes(tag)} presentational />
                    {isSkillTag(tag) ? <Puzzle className="size-3.5 shrink-0 text-violet-ink" /> : <Tag className="size-3.5 shrink-0 text-muted" />}
                    <span className="min-w-0 flex-1 break-words">{skillName(tag)}</span>
                    <span className="text-xs tabular-nums text-muted">{count}</span>
                  </button>
                ))}
              </fieldset>
            ) : null;
          })}
          {matches.length === 0 ? <p className="px-3 py-6 text-center text-sm text-muted">{t('vaults.filter.noTags')}</p> : null}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-muted">
          <span>{t('vaults.filter.intersection')}</span>
          <Button variant="ghost" size="sm" onClick={onClear}>{t('vaults.filter.clear')}</Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Two chips inline, skills first. The overflow is the same filter surface, not a tooltip. */
export function VaultRowTags({ secret, selected, onToggle }: {
  secret: VaultSecret;
  selected: string[];
  onToggle: (tag: string) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const restoringFocus = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cancelClose = () => clearTimeout(closeTimer.current);
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  const closeAfterPointerLeaves = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      if (document.activeElement !== trigger.current && !content.current?.contains(document.activeElement)) setOpen(false);
    }, 150);
  };
  const onBlur = (event: React.FocusEvent) => {
    const next = event.relatedTarget as Node | null;
    if (next !== trigger.current && !content.current?.contains(next)) setOpen(false);
  };
  const ordered = orderedTags(secret);
  const rest = ordered.slice(2);
  return (
    <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1.5">
      {ordered.slice(0, 2).map((tag) => <VaultFilterTag key={tag} tag={tag} active={selected.includes(tag)} onToggle={onToggle} />)}
      {rest.length > 0 ? (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              ref={trigger}
              type="button"
              className={cn(badgeVariants({ variant: 'outline' }), 'text-muted hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring')}
              aria-label={t('vaults.filter.moreTags', { count: rest.length })}
              onMouseEnter={() => { cancelClose(); setOpen(true); }}
              onMouseLeave={closeAfterPointerLeaves}
              onFocus={() => { cancelClose(); if (!restoringFocus.current) setOpen(true); }}
              onBlur={onBlur}
              // Focus/hover may already have opened it before the click arrives.
              onClick={(event) => { event.preventDefault(); setOpen(true); }}
              onKeyDown={(event) => {
                if (open && (event.key === 'Tab' && !event.shiftKey || event.key === 'ArrowDown')) {
                  event.preventDefault();
                  content.current?.querySelector<HTMLButtonElement>('button')?.focus();
                }
              }}
            >
              +{rest.length}
            </button>
          </PopoverTrigger>
          <PopoverContent
            ref={content}
            align="end"
            className="flex max-w-[calc(100vw-2rem)] flex-wrap gap-1.5 p-2"
            aria-label={t('vaults.filter.moreTags', { count: rest.length })}
            onMouseEnter={cancelClose}
            onMouseLeave={closeAfterPointerLeaves}
            onBlur={onBlur}
            onEscapeKeyDown={() => {
              restoringFocus.current = true;
              trigger.current?.focus();
              restoringFocus.current = false;
            }}
            onOpenAutoFocus={(event) => event.preventDefault()}
            onCloseAutoFocus={(event) => event.preventDefault()}
          >
            {rest.map((tag) => <VaultFilterTag key={tag} tag={tag} active={selected.includes(tag)} onToggle={onToggle} />)}
          </PopoverContent>
        </Popover>
      ) : null}
    </div>
  );
}

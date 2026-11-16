// Proposal: a secret's fields on its Vaults card instead of the "…" menu's native "Fields to show"
// select (owner, 2026-11-16 17:10). Three options for the owner to pick from (ui/design:
// vault/field-reveal/*). Product tokens and src/components/ui primitives only; copy is draft copy.
//
// For Protected, Show and Copy open the separate secure window and the value is shown (and copied)
// there: the card never shows a value inline and Avibe never receives it. Standard values have no Web
// reveal today; a Standard card lists its field names only.
import { ChevronDown, Copy, ExternalLink, Eye, KeyRound, MoreHorizontal, Pencil, Trash2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { useDraft } from './draft';

export type FieldRevealOption = 'rows' | 'expand' | 'menu';

export type FieldRevealSecret = {
  name: string;
  description: string;
  protection: 'standard' | 'protected';
  fields: string[];
  defaultField: string;
  tags: string[];
  used: string;
};

const ICON_BUTTON = 'inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-wash hover:text-foreground';

/** One field: its name and Show / Copy right after it (the same pattern as an address row and its
 *  copy button). Protected only: both open the secure window from the click, and the card never draws
 *  the value, not even masked. */
function FieldRow({ name, isDefault, single }: { name: string; isDefault: boolean; single: boolean }) {
  const d = useDraft();
  const label = single ? d('值', 'Value') : name;
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className={cn('flex w-[140px] shrink-0 items-center gap-1 truncate text-[11.5px] text-muted', !single && 'font-mono')}>
        {label}
        {isDefault && !single ? <span className="text-[10.5px] text-muted">· {d('默认', 'default')}</span> : null}
      </span>
      <span className="flex shrink-0 items-center">
        <button type="button" className={ICON_BUTTON} aria-label={d(`在安全窗口中显示 ${label}`, `Show ${label} in the secure window`)} title={d('在安全窗口中显示', 'Show in the secure window')}>
          <Eye className="size-3.5" />
        </button>
        <button type="button" className={ICON_BUTTON} aria-label={d(`在安全窗口中复制 ${label}`, `Copy ${label} in the secure window`)} title={d('在安全窗口中复制', 'Copy in the secure window')}>
          <Copy className="size-3.5" />
        </button>
      </span>
    </div>
  );
}

/** The one line that says where a protected value appears. */
function WindowNote() {
  const d = useDraft();
  return (
    <span className="flex items-center gap-1 text-[11px] text-muted">
      <ExternalLink className="size-3" />
      {d('值只在独立的安全窗口里显示和复制，Avibe 拿不到它。', 'Values are shown and copied only in the separate secure window; Avibe never receives them.')}
    </span>
  );
}

/** A Standard secret's field names: no value and no buttons (no Web reveal for Standard). */
function StandardFields({ secret }: { secret: FieldRevealSecret }) {
  const d = useDraft();
  if (secret.fields.length < 2) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {secret.fields.map((field) => (
        <Badge key={field} variant="outline" className="font-mono">
          {field}{field === secret.defaultField ? ` · ${d('默认', 'default')}` : ''}
        </Badge>
      ))}
    </div>
  );
}

function ProtectedRows({ secret }: { secret: FieldRevealSecret }) {
  const single = secret.fields.length === 1;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {secret.fields.map((field) => <FieldRow key={field} name={field} isDefault={field === secret.defaultField} single={single} />)}
      <WindowNote />
    </div>
  );
}

/** The "…" menu as option C draws it: each field a row with Show / Copy, then Edit / Delete. */
function MenuPanel({ secret }: { secret: FieldRevealSecret }) {
  const d = useDraft();
  const single = secret.fields.length === 1;
  const item = 'flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-wash';
  return (
    <div role="menu" className="absolute right-3 top-14 z-10 flex w-64 flex-col rounded-lg border border-edge bg-surface p-1 shadow-elevation-2">
      {single ? (
        <>
          <button type="button" role="menuitem" className={item}><Eye className="size-4 text-muted" />{d('在安全窗口中显示', 'Show in the secure window')}</button>
          <button type="button" role="menuitem" className={item}><Copy className="size-4 text-muted" />{d('在安全窗口中复制', 'Copy in the secure window')}</button>
        </>
      ) : (
        <>
          <span className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium text-muted">{d('在安全窗口中显示或复制', 'Show or copy in the secure window')}</span>
          {secret.fields.map((field) => (
            <div key={field} className="flex items-center gap-1 rounded-md px-2.5 py-1 hover:bg-wash">
              <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-foreground">{field}</span>
              <button type="button" className={ICON_BUTTON} aria-label={d(`显示 ${field}`, `Show ${field}`)}><Eye className="size-3.5" /></button>
              <button type="button" className={ICON_BUTTON} aria-label={d(`复制 ${field}`, `Copy ${field}`)}><Copy className="size-3.5" /></button>
            </div>
          ))}
        </>
      )}
      <div className="my-1 h-px bg-edge" />
      <button type="button" role="menuitem" className={item}><Pencil className="size-4 text-muted" />{d('编辑', 'Edit')}</button>
      <button type="button" role="menuitem" className={cn(item, 'text-destructive-ink')}><Trash2 className="size-4" />{d('删除', 'Delete')}</button>
    </div>
  );
}

/** A Vaults card with the option's field area. `menuOpen` draws option C's menu open. */
export function FieldRevealCard({ secret, option, menuOpen = false, expanded = false }: {
  secret: FieldRevealSecret;
  option: FieldRevealOption;
  menuOpen?: boolean;
  expanded?: boolean;
}) {
  const d = useDraft();
  const isProtected = secret.protection === 'protected';
  const single = secret.fields.length === 1;
  let fieldArea = null;
  if (!isProtected) fieldArea = <StandardFields secret={secret} />;
  else if (option === 'rows') fieldArea = <ProtectedRows secret={secret} />;
  else if (option === 'expand') {
    fieldArea = single ? null : (
      <div className="flex min-w-0 flex-col gap-1">
        <button type="button" aria-expanded={expanded} className="flex w-fit items-center gap-1 rounded-md py-0.5 text-[12px] font-medium text-foreground hover:text-accent-ink">
          <ChevronDown className={cn('size-3.5 text-muted transition-transform', !expanded && '-rotate-90')} />
          {d(`${secret.fields.length} 个字段`, `${secret.fields.length} fields`)}
          {!expanded ? <span className="font-mono text-[11.5px] font-normal text-muted">· {secret.fields.join(' · ')}</span> : null}
        </button>
        {expanded ? <ProtectedRows secret={secret} /> : null}
      </div>
    );
  } else {
    fieldArea = single ? null : (
      <div className="flex flex-wrap items-center gap-1">
        {secret.fields.map((field) => (
          <Badge key={field} variant="outline" className="font-mono">{field}{field === secret.defaultField ? ` · ${d('默认', 'default')}` : ''}</Badge>
        ))}
      </div>
    );
  }
  // Option B keeps a one-field protected secret one tap away: Show and Copy beside the menu.
  const inlineActions = isProtected && single && option === 'expand';
  return (
    <li className="relative flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-xl border border-edge bg-surface px-4 py-3 shadow-elevation-0 md:flex-nowrap">
      <div className="flex size-9 shrink-0 items-center justify-center self-start rounded-lg bg-accent/10 text-accent-ink">
        <KeyRound className="size-4" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{secret.name}</span>
          {isProtected ? <Badge variant="warning">{d('保护档', 'Protected')}</Badge> : <Badge variant="secondary">{d('标准档', 'Standard')}</Badge>}
        </div>
        <span className="truncate text-xs text-muted">{secret.description} · {secret.used}</span>
        {fieldArea}
      </div>
      <div className="order-last flex w-full min-w-0 flex-wrap items-center gap-1.5 md:order-none md:w-auto md:flex-col md:items-end">
        {secret.tags.map((tag) => <Badge key={tag} variant="outline">{tag}</Badge>)}
      </div>
      <div className="flex shrink-0 items-center self-start">
        {inlineActions ? (
          <>
            <button type="button" className={ICON_BUTTON} title={d('在安全窗口中显示', 'Show in the secure window')}><Eye className="size-4" /></button>
            <button type="button" className={ICON_BUTTON} title={d('在安全窗口中复制', 'Copy in the secure window')}><Copy className="size-4" /></button>
          </>
        ) : null}
        <Button variant="ghost" size="icon" aria-label={d('操作', 'Actions')}><MoreHorizontal className="size-4" /></Button>
      </div>
      {menuOpen ? <MenuPanel secret={secret} /> : null}
    </li>
  );
}

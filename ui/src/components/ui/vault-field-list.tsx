import { useId, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, Eye, EyeOff, LayoutTemplate, MoreHorizontal, Plus, X } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import type { VaultFieldNameIssue } from '@/lib/vaultFields';
import { Badge } from './badge';
import { badgeVariants } from './badge-variants';
import { Button } from './button';
import { Input } from './input';
import { Popover, PopoverContent, PopoverTrigger } from './popover';
import { ResponsiveMenu } from './responsive-menu';

// The field list shared by secret creation and editing. Rows lay out by the list's own width
// (`@container/fields`), not the viewport, so a narrow dialog stacks them the same way a phone
// does. Tailwind needs whole class names, so the 25rem breakpoint is spelled out in each one.

/** Field names whose values are identifiers rather than secrets, so they start revealed. */
const REVEALED_BY_DEFAULT = new Set(['username', 'email']);

const MENU_ITEM = 'flex w-full flex-col items-start gap-0.5 rounded-md px-3 py-2 text-left transition-colors hover:bg-foreground/[0.04] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent';

export function VaultFieldsHeader({ count, note }: { count: number; note?: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
      <span className="flex items-center gap-2 text-[13px] font-medium text-foreground">
        {t('vaults.fields.title')}
        <span className="rounded-md bg-surface-3 px-1.5 text-[11px] font-semibold tabular-nums text-muted">{count}</span>
      </span>
      {note ? <span className="text-xs text-muted">{note}</span> : null}
    </div>
  );
}

/** One bordered list with hairline dividers; adding a field is its last row. */
export function VaultFieldList({ children, onAdd, addDisabled }: {
  children: ReactNode;
  onAdd: () => void;
  addDisabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="@container/fields overflow-hidden rounded-xl border border-border bg-card">
      <div className="divide-y divide-border">{children}</div>
      <button
        type="button"
        onClick={onAdd}
        disabled={addDisabled}
        className="flex w-full items-center gap-1.5 border-t border-dashed border-border-strong px-4 py-2.5 text-[13px] font-medium text-mint-ink transition-colors hover:bg-mint-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
      >
        <Plus className="size-4" />
        {t('vaults.fields.add')}
      </button>
    </div>
  );
}

type FieldNameSpec =
  | {
      editable: true;
      value: string;
      label: string;
      onChange: (value: string) => void;
      onFocus?: () => void;
      onBlur?: () => void;
      autoFocus?: boolean;
    }
  | { editable: false; title: string };

type FieldValueSpec = {
  value: string;
  label: string;
  placeholder: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  autoFocus?: boolean;
};

type FieldRemoveSpec = {
  label: string;
  /** Removing waits for this inline confirmation; omit it for a row that holds nothing saved. */
  confirm?: ReactNode;
  onRemove: () => unknown;
  /** Keeps the remove control in place but inert, and says why. */
  blockedReason?: string;
};

export function VaultFieldRow({
  field, name, isDefault, value, action, onSetDefault, remove, issue, disabled,
}: {
  /** The row's current name, used in labels and the phone menu's title. */
  field: string;
  name: FieldNameSpec;
  isDefault: boolean;
  value: FieldValueSpec;
  /** Trailing control beside the value, such as a row's Save button. */
  action?: ReactNode;
  onSetDefault?: () => void;
  remove?: FieldRemoveSpec;
  /** What keeps this row from saving; the parent decides when a blank one is worth showing. */
  issue?: VaultFieldNameIssue | 'valueRequired' | null;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  // A name issue concerns the name as typed (a pending rename may differ from `field`), and
  // each issue marks only the input it is about.
  const typedName = name.editable ? name.value : field;
  const error = issue === 'nameRequired' ? t('vaults.fields.nameRequired')
    : issue === 'nameInvalid' ? t('vaults.fields.nameInvalid')
      : issue === 'nameTaken' ? t('vaults.fields.nameTaken', { field: typedName })
        : issue === 'valueRequired' ? t('vaults.fields.valueRequired')
          : null;
  const errorId = useId();
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const nameInvalid = Boolean(issue) && issue !== 'valueRequired';
  const valueInvalid = issue === 'valueRequired';
  const canSetDefault = !isDefault && Boolean(onSetDefault);

  const requestRemove = () => {
    if (!remove || remove.blockedReason) return;
    if (remove.confirm) setConfirming(true);
    else void remove.onRemove();
  };
  const confirmRemove = async () => {
    if (!remove) return;
    setRemoving(true);
    try {
      await remove.onRemove();
    } finally {
      setRemoving(false);
      setConfirming(false);
    }
  };

  if (confirming && remove?.confirm) {
    return (
      <div className="flex flex-wrap items-center gap-2 bg-destructive/[0.06] px-3 py-2.5">
        <span className="min-w-0 text-[13px] text-destructive-ink wrap-anywhere">{remove.confirm}</span>
        <span className="ml-auto flex gap-1">
          <Button type="button" variant="ghost" size="xs" autoFocus disabled={removing} onClick={() => setConfirming(false)}>
            {t('vaults.dialog.cancel')}
          </Button>
          <Button type="button" variant="destructive" size="xs" disabled={removing || disabled} onClick={() => void confirmRemove()}>
            {t('vaults.fields.confirmRemove')}
          </Button>
        </span>
      </div>
    );
  }

  return (
    <div className={cn('group/field px-2.5 py-2 transition-colors', error && 'bg-destructive/[0.04]')}>
      <div className="flex flex-col gap-1.5 @[25rem]/fields:flex-row @[25rem]/fields:items-center @[25rem]/fields:gap-2">
        <div className="flex min-w-0 items-center gap-1.5 @[25rem]/fields:w-[38%] @[25rem]/fields:shrink-0">
          {name.editable ? (
            <Input
              variant="bare"
              value={name.value}
              onChange={(event) => name.onChange(event.target.value)}
              onFocus={name.onFocus}
              onBlur={name.onBlur}
              autoFocus={name.autoFocus}
              aria-label={name.label}
              aria-invalid={nameInvalid || undefined}
              aria-describedby={nameInvalid ? errorId : undefined}
              placeholder={t('vaults.fields.namePlaceholder')}
              maxLength={64}
              spellCheck={false}
              autoComplete="off"
              disabled={disabled}
              className={cn(
                'h-8 min-w-0 flex-1 rounded-md border border-transparent px-2 font-mono text-[13px] transition-colors hover:border-input focus:bg-background focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring',
                nameInvalid && 'border-destructive/50 hover:border-destructive/50',
              )}
            />
          ) : (
            <span title={name.title} className="min-w-0 flex-1 truncate px-2 font-mono text-[13px] leading-8 text-foreground">{field}</span>
          )}
          {isDefault ? (
            <Badge variant="success" className="shrink-0 px-2">{t('vaults.fields.default')}</Badge>
          ) : canSetDefault ? (
            <button
              type="button"
              onClick={onSetDefault}
              disabled={disabled}
              aria-label={t('vaults.fields.setDefaultNamed', { field })}
              className={cn(
                badgeVariants({ variant: 'outline' }),
                'hidden shrink-0 border-dashed px-2 font-medium tracking-normal text-muted opacity-0 hover:border-mint hover:text-mint-ink focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-focus-within/field:opacity-100 group-hover/field:opacity-100 disabled:opacity-0 @[25rem]/fields:inline-flex',
              )}
            >
              {t('vaults.fields.setDefault')}
            </button>
          ) : null}
          {canSetDefault || remove ? (
            <div className="ml-auto shrink-0 @[25rem]/fields:hidden">
              <ResponsiveMenu
                open={menuOpen}
                onOpenChange={setMenuOpen}
                sheetTitle={field}
                className="w-56"
                trigger={(
                  <Button type="button" variant="ghost" size="icon" disabled={disabled}
                    aria-label={t('vaults.fields.rowActions', { field })}
                    className="size-8 text-muted hover:text-foreground">
                    <MoreHorizontal className="size-4" />
                  </Button>
                )}
              >
                {canSetDefault ? (
                  <button type="button" role="menuitem" className={MENU_ITEM}
                    onClick={() => { setMenuOpen(false); onSetDefault?.(); }}>
                    <span className="text-[13px] text-foreground">{t('vaults.fields.setDefault')}</span>
                  </button>
                ) : null}
                {remove ? (
                  <button type="button" role="menuitem" className={MENU_ITEM} disabled={Boolean(remove.blockedReason)}
                    onClick={() => { setMenuOpen(false); requestRemove(); }}>
                    <span className="text-[13px] text-destructive-ink">{t('vaults.fields.removeField')}</span>
                    {remove.blockedReason ? <span className="text-[11px] text-muted">{remove.blockedReason}</span> : null}
                  </button>
                ) : null}
              </ResponsiveMenu>
            </div>
          ) : null}
        </div>
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <VaultFieldValueInput field={field} {...value} disabled={disabled} invalid={valueInvalid}
            describedBy={valueInvalid ? errorId : undefined} />
          {action}
          {remove ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              disabled={disabled}
              aria-label={remove.label}
              aria-disabled={remove.blockedReason ? true : undefined}
              title={remove.blockedReason}
              onClick={requestRemove}
              className={cn(
                'hidden size-7 shrink-0 text-muted opacity-0 focus-visible:opacity-100 @[25rem]/fields:inline-flex',
                remove.blockedReason
                  ? 'cursor-not-allowed hover:bg-transparent group-focus-within/field:opacity-40 group-hover/field:opacity-40'
                  : 'hover:bg-destructive/10 hover:text-destructive-ink group-focus-within/field:opacity-100 group-hover/field:opacity-100',
              )}
            >
              <X className="size-4" />
            </Button>
          ) : (
            <span aria-hidden className="hidden size-7 shrink-0 @[25rem]/fields:block" />
          )}
        </div>
      </div>
      {error ? <p id={errorId} className="mt-1 px-2 text-xs text-destructive-ink wrap-anywhere">{error}</p> : null}
    </div>
  );
}

function VaultFieldValueInput({
  field, value, label, placeholder, onChange, onBlur, autoFocus, disabled, invalid, describedBy,
}: FieldValueSpec & { field: string; disabled?: boolean; invalid?: boolean; describedBy?: string }) {
  const { t } = useTranslation();
  // Unset until the user toggles, so a row renamed to `username` follows its new name.
  const [reveal, setReveal] = useState<boolean | null>(null);
  const revealed = reveal ?? REVEALED_BY_DEFAULT.has(field);
  return (
    <div className="relative min-w-0 flex-1">
      <Input
        type={revealed ? 'text' : 'password'}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        autoFocus={autoFocus}
        aria-label={label}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        className={cn('pr-9', invalid && 'border-destructive/50')}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        disabled={disabled}
        onClick={() => setReveal(!revealed)}
        aria-label={revealed ? t('vaults.fields.hideNamed', { field }) : t('vaults.fields.showNamed', { field })}
        className="absolute right-1 top-1/2 size-7 -translate-y-1/2 text-muted hover:text-foreground"
      >
        {revealed ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </Button>
    </div>
  );
}

/** How an agent names each field, so the default field's bare-name shorthand is visible. */
export function VaultFieldReferences({ secretName, fields, defaultField }: {
  secretName: string;
  fields: string[];
  defaultField: string;
}) {
  const { t } = useTranslation();
  if (fields.length === 0) return null;
  const name = secretName || t('vaults.fields.referenceName');
  return (
    <div className="flex flex-col gap-1.5 rounded-lg bg-background px-3 py-2.5 text-xs text-muted">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-0.5">{t('vaults.fields.references')}</span>
        {fields.map((field) => (
          <code
            key={field}
            className={cn(
              'min-w-0 rounded-md px-1.5 py-0.5 font-mono text-[11px] ring-1 wrap-anywhere',
              field === defaultField ? 'bg-mint-soft text-mint-ink ring-mint/30' : 'bg-card text-foreground ring-border',
            )}
          >
            {name}.{field}
          </code>
        ))}
      </div>
      {fields.includes(defaultField) ? (
        <span className="wrap-anywhere">
          <Trans
            i18nKey="vaults.fields.referenceDefault"
            components={{
              name: <code className="font-mono text-[11px] text-foreground">{name}</code>,
              field: <code className="font-mono text-[11px] text-mint-ink">{defaultField}</code>,
            }}
          />
        </span>
      ) : null}
    </div>
  );
}

/** Inline removal copy with the field name set in mono. */
export function VaultFieldRemoveConfirm({ field, revokesGrants }: { field: string; revokesGrants: boolean }) {
  const code = <code className="font-mono">{field}</code>;
  return revokesGrants
    ? <Trans i18nKey="vaults.fields.removeConfirmSaved" components={{ field: code }} />
    : <Trans i18nKey="vaults.fields.removeConfirm" components={{ field: code }} />;
}

/** Field presets for a single value; the value already typed moves into the default field. */
export function VaultFieldTemplateMenu({ disabled, onApply }: {
  disabled?: boolean;
  onApply: (fields: string[], defaultField: string) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const menuId = useId();
  // The trigger unmounts once a template applies, so focus goes to the new rows instead.
  const appliedRef = useRef(false);
  const templates = [
    { label: t('vaults.fields.templateLogin'), fields: ['username', 'password'] },
    { label: t('vaults.fields.templateLogin2fa'), fields: ['username', 'password', 'totp'] },
  ];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="xs" disabled={disabled} aria-haspopup="menu"
          className="px-2.5 text-[13px] text-muted hover:text-foreground">
          <LayoutTemplate />
          {t('vaults.fields.useTemplate')}
          <ChevronDown className="opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-64 p-1"
        role="menu"
        onCloseAutoFocus={(event) => {
          if (appliedRef.current) event.preventDefault();
          appliedRef.current = false;
        }}
      >
        {templates.map((template, index) => (
          <button
            key={template.fields.join('.')}
            type="button"
            role="menuitem"
            aria-label={template.label}
            aria-describedby={`${menuId}-${index}`}
            className={MENU_ITEM}
            onClick={() => {
              appliedRef.current = true;
              setOpen(false);
              onApply(template.fields, 'password');
            }}
          >
            <span className="text-sm font-medium text-foreground">{template.label}</span>
            <span id={`${menuId}-${index}`} className="font-mono text-[11px] text-muted">{template.fields.join(' · ')}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

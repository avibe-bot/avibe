// The complete Add / Agent request / Edit dialogs with the generator in place (avibe#3338), drawn
// field for field as the shipped VaultSecretDialog draws them today (vault-secret-form.tsx): every
// existing field is here and unchanged, using the shipped copy (`t`); only the value area is new.
import type { ReactNode } from 'react';
import { Asterisk, Check, Eye, EyeOff, KeyRound, Lock, Pencil, Plus, Server, ShieldCheck, Signature, SlidersHorizontal, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { TagInput } from '@/components/ui/tag-input';
import { VaultFieldList, VaultFieldReferences, VaultFieldRow, VaultFieldsHeader, VaultFieldTemplateMenu } from '@/components/ui/vault-field-list';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { DEFAULT_PASSWORD, SAMPLE, type Recipe } from './data';
import {
  DiceButton, GeneratedChip, GeneratedValue, GeneratorOptions, OptionsLine, ProtectedGenerateNote, RecipeBanner, TierSwitchedNote,
} from './parts';

const noop = () => {};
const FIELD_LABEL = 'text-[13px] font-medium text-foreground';
export type Tier = 'standard' | 'protected';
export type Variant = 'A' | 'B';

/** What the value area shows in a frame. */
export type ValueState =
  | { kind: 'generated'; recipe: Recipe; value: string; shown?: boolean; optionsOpen?: boolean }
  | { kind: 'protected'; recipe: Recipe; switched?: boolean; optionsOpen?: boolean }
  | { kind: 'typed'; value: string };

function Header({ mode }: { mode: 'create' | 'provide' | 'edit' }) {
  const { t } = useTranslation();
  const Icon = mode === 'edit' ? Pencil : KeyRound;
  const title = mode === 'edit' ? t('vaults.edit.title') : mode === 'provide' ? t('vaults.request.title') : t('vaults.dialog.title');
  const subtitle = mode === 'edit' ? t('vaults.edit.subtitle') : mode === 'provide' ? t('vaults.request.help') : t('vaults.dialog.subtitle');
  return (
    <div className="flex min-w-0 items-start gap-3 pr-6">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent-ink"><Icon className="size-5" /></span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="break-words text-[15px] font-semibold text-foreground">{title}</span>
        <span className="break-words text-xs text-muted">{subtitle}</span>
      </div>
    </div>
  );
}

function TierCards({ value }: { value: Tier }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.protection')}</span>
      <div className="grid grid-cols-2 gap-2.5">
        {([
          { key: 'standard', icon: Server, title: t('vaults.dialog.standardProtection'), desc: t('vaults.dialog.standardHelp') },
          { key: 'protected', icon: ShieldCheck, title: t('vaults.dialog.protectedProtection'), desc: t('vaults.dialog.protectedHelp') },
        ] as const).map(({ key, icon: Icon, title, desc }) => {
          const selected = value === key;
          return (
            <button key={key} type="button" aria-pressed={selected}
              className={cn('flex flex-col gap-1.5 rounded-[10px] border p-3 text-left transition-colors', selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-edge bg-field')}>
              <span className="flex items-center gap-2">
                <Icon className={cn('size-4', selected ? 'text-mint-ink' : 'text-muted')} />
                <span className="flex-1 text-[13.5px] font-semibold text-foreground">{title}</span>
                {selected && <Check className="size-4 text-mint-ink" />}
              </span>
              <span className="text-[11.5px] leading-snug text-muted">{desc}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Description({ value = '' }: { value?: string }) {
  const { t } = useTranslation();
  return (
    <label className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
      <Input readOnly value={value} placeholder={t('vaults.dialog.descriptionPlaceholder')} />
    </label>
  );
}

function Tags({ values = [] }: { values?: string[] }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.tags')}</span>
      <TagInput values={values} onChange={noop} placeholder={t('vaults.dialog.tagsPlaceholder')} ariaLabel={t('vaults.dialog.tags')} removeLabel={(v) => t('vaults.dialog.removeChip', { value: v })} />
      <span className="text-[11px] text-muted">{t('vaults.dialog.tagsHelp')}</span>
    </div>
  );
}

function Advanced({ dot = false }: { dot?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col overflow-hidden rounded-[10px] bg-surface-2">
      <button type="button" aria-expanded={false} className="flex items-center gap-1.5 px-3 py-2.5 text-left">
        <SlidersHorizontal className="size-3.5 text-muted" />
        <span className="flex-1 text-xs font-semibold text-foreground">{t('vaults.dialog.advanced')}</span>
        {dot ? <span className="size-1.5 rounded-full bg-mint" aria-hidden /> : null}
      </button>
    </div>
  );
}

function AddFieldsLine() {
  const { t } = useTranslation();
  return (
    <>
      <div className="-ml-2.5 flex flex-wrap items-center gap-0.5">
        <Button type="button" variant="ghost" size="xs" className="px-2.5 text-[13px] text-muted hover:text-foreground"><Plus />{t('vaults.fields.add')}</Button>
        <VaultFieldTemplateMenu onApply={noop} />
      </div>
      <span className="text-[11px] text-muted">{t('vaults.fields.multiHint')}</span>
    </>
  );
}

/** The single value, as variant A (a third value source) or B (a dice button in the field) draws it. */
function SingleValue({ variant, tier, state }: { variant: Variant; tier: Tier; state: ValueState }) {
  const { t } = useTranslation();
  const d = useDraft();
  const generating = state.kind !== 'typed';
  if (variant === 'A') {
    // Standard keeps Text and File and adds Generate; Protected has no File today, so Type | Generate.
    const options = tier === 'standard'
      ? [{ id: 'text', label: t('vaults.dialog.valueSourceText') }, { id: 'file', label: t('vaults.dialog.valueSourceFile') }, { id: 'generate', label: d('生成', 'Generate') }]
      : [{ id: 'text', label: d('输入', 'Type') }, { id: 'generate', label: d('生成', 'Generate') }];
    return (
      <div className="flex flex-col gap-2">
        <div className="self-start">
          <SegmentedRadio<string> value={generating ? 'generate' : 'text'} onChange={noop} ariaLabel={t('vaults.dialog.valueSource')} options={options} />
        </div>
        {state.kind === 'typed' ? (
          <Input type="password" readOnly value={state.value} placeholder={t('vaults.dialog.valuePlaceholder')} className="font-mono text-xs" />
        ) : (
          <div className="flex flex-col gap-3 border-l-2 border-mint/40 py-1 pl-3">
            <GeneratorOptions recipe={state.recipe} />
            {state.kind === 'generated' ? <GeneratedValue value={state.value} shown={state.shown} /> : <ProtectedGenerateNote />}
            {state.kind === 'protected' && state.switched ? <TierSwitchedNote /> : null}
          </div>
        )}
      </div>
    );
  }
  // B: the field stays; the dice beside the eye fills it.
  return (
    <div className="flex flex-col gap-2">
      {tier === 'standard' ? (
        <div className="self-start">
          <SegmentedRadio<string> value="text" onChange={noop} ariaLabel={t('vaults.dialog.valueSource')}
            options={[{ id: 'text', label: t('vaults.dialog.valueSourceText') }, { id: 'file', label: t('vaults.dialog.valueSourceFile') }]} />
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        {state.kind === 'protected' ? (
          <span className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-md border border-mint/40 bg-mint-soft/40 px-3 text-[12px] text-foreground">
            <Lock className="size-3.5 shrink-0 text-mint-ink" />{d('保存时在安全窗口里生成', 'Generated in the secure window when you save')}
          </span>
        ) : (
          <Input type={state.kind === 'generated' && state.shown ? 'text' : 'password'} readOnly value={state.value} placeholder={t('vaults.dialog.valuePlaceholder')}
            className={cn('min-w-0 flex-1 font-mono text-xs', state.kind === 'generated' && 'border-mint/40 bg-mint-soft/40')} />
        )}
        <DiceButton active={generating} />
        {state.kind !== 'protected' ? <Button type="button" variant="ghost" size="icon" aria-label={t('vaults.dialog.showValue')}>{state.kind === 'generated' && state.shown ? <EyeOff className="size-4" /> : <Eye className="size-4" />}</Button> : null}
      </div>
      {state.kind === 'generated' ? (
        <>
          <div className="flex flex-wrap items-center gap-2"><GeneratedChip /><span className="text-[11px] text-muted">{d('再点骰子换一个；直接输入就改成你自己的值。', 'Tap the dice again for another; type to use your own instead.')}</span></div>
          <OptionsLine recipe={state.recipe} open={state.optionsOpen} />
        </>
      ) : state.kind === 'protected' ? (
        <>
          <OptionsLine recipe={state.recipe} open={state.optionsOpen} />
          <span className="text-[11px] text-muted">{d('这个页面和 Agent 都看不到这个值；保存后可以用「显示值」在安全窗口里查看。', 'Neither this page nor any Agent sees the value; after saving, use Show value to see it in the secure window.')}</span>
          {state.switched ? <TierSwitchedNote /> : null}
        </>
      ) : null}
    </div>
  );
}

/** Add a secret, static, single value: every field of the shipped create form. */
export function CreateDialog({ variant, tier, state, name = 'DB_PASSWORD' }: { variant: Variant; tier: Tier; state: ValueState; name?: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="create" />
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio<string> value="static" onChange={noop} ariaLabel={t('vaults.dialog.kindLabel')}
          options={[{ id: 'static', label: t('vaults.dialog.kindStatic'), icon: KeyRound }, { id: 'keypair', label: t('vaults.dialog.kindKeypair'), icon: Signature }]} />
      </div>
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
        <Input readOnly value={name} className="font-mono" />
        <span className="text-[11.5px] text-muted">{t('vaults.dialog.nameHint')}</span>
      </label>
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
        <SingleValue variant={variant} tier={tier} state={state} />
        <AddFieldsLine />
      </div>
      <Description />
      <Tags />
      <TierCards value={tier} />
      <Advanced />
      <Footer primary={t('vaults.dialog.createSecret')} />
    </div>
  );
}

/** Add a secret with several fields: generate on one row; the others stay typed. */
export function CreateFieldsDialog({ variant, tier }: { variant: Variant; tier: Tier }) {
  const { t } = useTranslation();
  const d = useDraft();
  const row = (field: string, value: string, extra: { generated?: boolean; isDefault?: boolean } = {}) => (
    <VaultFieldRow
      key={field}
      field={field}
      isDefault={Boolean(extra.isDefault)}
      name={{ editable: true, value: field, label: t('vaults.fields.name', { index: 1 }), onChange: noop }}
      value={{ value, label: t('vaults.fields.value', { field }), placeholder: t('vaults.fields.value', { field }), onChange: noop }}
      action={<DiceButton active={extra.generated} />}
      onSetDefault={noop}
    />
  );
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="create" />
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio<string> value="static" onChange={noop} ariaLabel={t('vaults.dialog.kindLabel')}
          options={[{ id: 'static', label: t('vaults.dialog.kindStatic'), icon: KeyRound }, { id: 'keypair', label: t('vaults.dialog.kindKeypair'), icon: Signature }]} />
      </div>
      <label className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
        <Input readOnly value="APP_ADMIN" className="font-mono" />
        <span className="text-[11.5px] text-muted">{t('vaults.dialog.nameHint')}</span>
      </label>
      <div className="flex flex-col gap-2">
        <VaultFieldsHeader count={2} />
        <VaultFieldList onAdd={noop}>
          {row('username', 'admin')}
          {tier === 'standard' ? row('password', SAMPLE.password, { generated: true, isDefault: true }) : (
            <VaultFieldRow field="password" isDefault
              name={{ editable: true, value: 'password', label: t('vaults.fields.name', { index: 2 }), onChange: noop }}
              value={{ value: '', label: t('vaults.fields.value', { field: 'password' }), placeholder: d('保存时生成', 'Generated on save'), onChange: noop }}
              action={<DiceButton active />} />
          )}
          <div className="flex flex-col gap-2 px-3 py-2.5">
            {tier === 'standard' ? <div className="flex flex-wrap items-center gap-2"><GeneratedChip /><span className="text-[11px] text-muted">{d('只生成了 password 这一行。', 'Only the password row was generated.')}</span></div> : null}
            {variant === 'A' ? <GeneratorOptions recipe={DEFAULT_PASSWORD} compact /> : <OptionsLine recipe={DEFAULT_PASSWORD} />}
            {tier === 'protected' ? <ProtectedGenerateNote /> : null}
          </div>
        </VaultFieldList>
        <VaultFieldReferences secretName="APP_ADMIN" fields={['username', 'password']} defaultField="password" />
      </div>
      <Description />
      <Tags />
      <TierCards value={tier} />
      <Advanced />
      <Footer primary={t('vaults.dialog.createSecret')} />
    </div>
  );
}

/** The Agent's request: its recipe sets up the generator; saving is still the user's click. */
export function ProvideDialog({ variant, tier, state }: { variant: Variant; tier: Tier; state: ValueState }) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="provide" />
      <div className="flex items-center gap-3 rounded-xl bg-accent/15 p-3.5">
        <Asterisk className="size-[18px] shrink-0 text-accent-ink" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">{t('vaults.dialog.nameUpper')}</span>
          <span className="whitespace-normal break-all font-mono text-[15px] font-semibold text-foreground">SESSION_SIGNING_KEY</span>
        </div>
        <Badge variant="secondary">{t('vaults.request.notSetYet')}</Badge>
      </div>
      {state.kind !== 'typed' ? <RecipeBanner recipe={state.recipe} /> : null}
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.value')}</span>
        <SingleValue variant={variant} tier={tier} state={state} />
        <AddFieldsLine />
        <span className="text-[11px] text-muted">{t('vaults.dialog.provisionValueHelp')}</span>
      </div>
      <Description value={d('Web 会话签名密钥', 'Web session signing key')} />
      <Tags values={['web']} />
      <TierCards value={tier} />
      <Advanced />
      <div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
        <Button type="button" variant="destructive-soft" className="w-full sm:mr-auto sm:w-auto"><X className="size-3.5" />{t('vaults.approval.deny')}</Button>
        <Button type="button" variant="ghost" className="w-full sm:w-auto">{t('vaults.request.dismiss')}</Button>
        <Button type="button" className="w-full sm:w-auto"><Check className="size-4" />{t('vaults.request.saveAndWake')}</Button>
      </div>
    </div>
  );
}

/** Edit a secret: a field row gets a generated replacement; it saves with that row's Save. */
export function EditFieldDialog({ variant, tier }: { variant: Variant; tier: Tier }) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="edit" />
      <div className="flex items-center gap-3 rounded-xl border border-edge bg-surface-2 p-3.5 shadow-elevation-0">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted/10 text-muted"><Lock className="size-4" /></span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="truncate font-mono text-[15px] font-semibold text-foreground">APP_ADMIN</span>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="secondary">{t('vaults.dialog.kindStatic')}</Badge>
            <Badge variant={tier === 'protected' ? 'warning' : 'secondary'}>{tier === 'protected' ? t('vaults.protected') : t('vaults.standard')}</Badge>
          </div>
        </div>
      </div>
      <p className="flex items-center gap-1.5 text-[11px] text-muted"><Lock className="size-3 shrink-0" />{t('vaults.edit.lockedHint')}</p>
      <div className="flex flex-col gap-2">
        <VaultFieldsHeader count={2} note={t('vaults.fields.editHelp')} />
        <VaultFieldList onAdd={noop}>
          <VaultFieldRow field="username" isDefault={false} name={{ editable: false, title: t('vaults.fields.extraNotRenamable') }}
            value={{ value: '', label: t('vaults.fields.value', { field: 'username' }), placeholder: t('vaults.fields.replacePlaceholder'), onChange: noop }}
            action={<DiceButton />} />
          <VaultFieldRow field="password" isDefault name={{ editable: true, value: 'password', label: t('vaults.fields.defaultName'), onChange: noop }}
            value={{ value: tier === 'standard' ? SAMPLE.password2 : '', label: t('vaults.fields.value', { field: 'password' }),
              placeholder: tier === 'standard' ? t('vaults.fields.replacePlaceholder') : d('保存时生成', 'Generated on save'), onChange: noop }}
            action={<><DiceButton active /><Button type="button" size="xs">{t('vaults.fields.save')}</Button></>} />
          <div className="flex flex-col gap-2 px-3 py-2.5">
            {tier === 'standard' ? (
              <div className="flex flex-wrap items-center gap-2"><GeneratedChip /><span className="text-[11px] text-muted">{d('点这一行的「保存」才会替换；取消或关闭，已保存的值不变。', 'It replaces the saved value only when you click this row’s Save; cancel or close and the saved value stays.')}</span></div>
            ) : <ProtectedGenerateNote />}
            {variant === 'A' ? <GeneratorOptions recipe={DEFAULT_PASSWORD} compact /> : <OptionsLine recipe={DEFAULT_PASSWORD} />}
          </div>
        </VaultFieldList>
        <VaultFieldReferences secretName="APP_ADMIN" fields={['username', 'password']} defaultField="password" />
      </div>
      <Description value={d('后台管理员账号', 'Admin console account')} />
      <Tags values={['prod']} />
      <Advanced />
      <Footer primary={t('vaults.dialog.save')} />
    </div>
  );
}

function Footer({ primary }: { primary: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="mt-1 flex justify-end gap-2">
      <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
      <Button type="button">{primary}</Button>
    </div>
  );
}

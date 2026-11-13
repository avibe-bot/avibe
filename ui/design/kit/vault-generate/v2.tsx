// Version 2 of the generate-secret proposal (avibe#3338; owner 2026-11-13 20:58, after 1Password):
// the generator belongs to the value field. A dice control sits at the field's end and, while the
// field has focus (always on a phone while the field is empty), it reads 「生成随机密码」. It opens a
// popup anchored to the field on a desktop and a bottom sheet on a phone (the shipped
// ResponsiveMenu, modal because it opens inside the modal dialog); 使用 puts the value in the field
// and nothing is saved until the dialog's own Save. Draft copy (kit/draft.ts); nothing here ships.
import type { ReactNode } from 'react';
import { Asterisk, Check, Dices, Eye, KeyRound, Lock, RefreshCw, Signature, Sparkles, WandSparkles, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Switch } from '@/components/ui/switch';
import { VaultFieldList, VaultFieldReferences, VaultFieldRow, VaultFieldsHeader } from '@/components/ui/vault-field-list';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';

import { useDraft } from '../draft';
import { HEX32, PASSWORD_24, SAMPLE, SAMPLE_BASE64, SYMBOLS, genBits, genChars, type Gen, type GenType } from './data';
import { AddFieldsLine, Advanced, Description, Footer, Header, Tags, TierCards, type Tier } from './dialogs';

const noop = () => {};
const LABEL = 'text-[13px] font-medium text-foreground';

const sampleFor = (g: Gen) => (g.type === 'password' ? SAMPLE.password : g.type === 'hex' ? SAMPLE.hex : SAMPLE_BASE64);

/** What a generator's settings make, in a few words: "Hex · 32 bytes = 64 characters". */
function GenSummary({ gen }: { gen: Gen }) {
  const d = useDraft();
  if (gen.type === 'password') return <>{d(`密码 · ${gen.length} 个字符`, `Password · ${gen.length} characters`)}</>;
  const name = gen.type === 'hex' ? 'Hex' : 'Base64';
  return <>{d(`${name} · ${gen.bytes} 字节 = ${genChars(gen)} 个字符`, `${name} · ${gen.bytes} bytes = ${genChars(gen)} characters`)}</>;
}

/** A labelled row of the popup: label on the left, control on the right, as in 1Password. */
function Row({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-11 items-center gap-3 border-t border-border py-2 first:border-t-0">
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-[13px] text-foreground">{label}</span>
        {hint ? <span className="text-[11px] text-muted">{hint}</span> : null}
      </span>
      {children}
    </div>
  );
}

/** Length: a slider for the hand, the number beside it for the keyboard (exact, same limits). */
function LengthRow({ gen }: { gen: Gen }) {
  const d = useDraft();
  const [min, max, value] = gen.type === 'password' ? [12, 128, gen.length] : [16, 64, gen.bytes];
  const label = gen.type === 'password'
    ? d(`${value} 个字符`, `${value} characters`)
    : d(`${value} 字节`, `${value} bytes`);
  return (
    <Row label={label} hint={gen.type === 'password' ? d('12–128', '12–128') : d(`= ${genChars(gen)} 个字符`, `= ${genChars(gen)} characters`)}>
      <input type="range" min={min} max={max} value={value} readOnly aria-label={d('长度', 'Length')}
        className="h-1.5 w-32 cursor-pointer accent-[var(--mint)] sm:w-36" />
      <Input type="number" readOnly value={value} aria-label={d('长度', 'Length')} className="h-8 w-14 px-2 text-center font-mono text-xs" />
    </Row>
  );
}

/** The popup's body: the value (Standard) or what Protected does instead, the type, its options. */
export function GeneratorPanel({ tier, gen, suggested = false }: { tier: Tier; gen: Gen; suggested?: boolean }) {
  const d = useDraft();
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  return (
    <div className={cn('flex flex-col gap-3', isMobile ? 'px-4 pb-1 pt-3' : 'p-2.5')}>
      {suggested ? (
        <span className="flex items-center gap-1.5 rounded-md bg-violet-soft px-2.5 py-1.5 text-[11.5px] text-violet-ink">
          <WandSparkles className="size-3.5 shrink-0" />
          <span>{d('按 Agent 的建议预设：', 'Preset from the Agent’s suggestion: ')}<GenSummary gen={gen} /></span>
        </span>
      ) : null}
      {tier === 'standard' ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-1.5 rounded-lg border border-edge bg-field px-3 py-2">
            <span className="min-w-0 flex-1 break-all font-mono text-[13px] leading-snug text-foreground">{sampleFor(gen)}</span>
            <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0" aria-label={d('换一个', 'Regenerate')}><RefreshCw className="size-4" /></Button>
          </div>
          <span className="text-[11px] text-muted">{d(`约 ${genBits(gen)} 位随机性`, `About ${genBits(gen)} bits of randomness`)}</span>
        </div>
      ) : (
        <div role="note" className="flex items-start gap-2 rounded-lg border border-mint/30 bg-mint/[0.06] px-3 py-2.5 text-[12px] leading-snug text-foreground">
          <Lock className="mt-0.5 size-3.5 shrink-0 text-mint-ink" />
          <span>{d('保护档的值只在安全窗口里出现：保存时在那里按这些设置生成，这个页面和 Agent 都看不到。', 'A Protected value only ever exists in the secure window: it is generated there with these settings when you save; neither this page nor any Agent sees it.')}</span>
        </div>
      )}
      <div className="flex flex-col">
        <Row label={d('类型', 'Type')}>
          <SegmentedRadio<GenType> value={gen.type} onChange={noop} ariaLabel={d('类型', 'Type')} className="h-8"
            options={[{ id: 'password', label: d('密码', 'Password') }, { id: 'hex', label: 'Hex' }, { id: 'base64', label: 'Base64' }]} />
        </Row>
        <LengthRow gen={gen} />
        {gen.type === 'password' ? (
          <>
            <Row label={d('数字', 'Digits')}><Switch checked={gen.digits} onCheckedChange={noop} label={d('数字', 'Digits')} /></Row>
            <Row label={d('符号', 'Symbols')} hint={<span className="font-mono">{SYMBOLS}</span>}><Switch checked={gen.symbols} onCheckedChange={noop} label={d('符号', 'Symbols')} /></Row>
            <Row label={d('排除易混字符', 'No look-alikes')} hint={d('不用 0 O 1 l I', 'Leaves out 0 O 1 l I')}><Switch checked={gen.noLookalike} onCheckedChange={noop} label={d('排除易混字符', 'No look-alikes')} /></Row>
          </>
        ) : gen.type === 'base64' ? (
          <span className="pt-1 text-[11px] text-muted">{d('标准 Base64（含 + / =），和 ', 'Standard Base64 (with + / =), the same as ')}<code className="whitespace-nowrap font-mono">openssl rand -base64</code>{d(' 输出的一样。', ' prints.')}</span>
        ) : null}
      </div>
      {isMobile ? (
        <Button type="button" className="h-12 w-full"><Check className="size-4" />{tier === 'standard' ? d('使用这个值', 'Use this value') : d('使用这些设置', 'Use these settings')}</Button>
      ) : (
        <div className="flex justify-end gap-2 border-t border-border pt-2.5">
          <Button type="button" variant="ghost" size="sm">{t('vaults.dialog.cancel')}</Button>
          <Button type="button" size="sm"><Check className="size-3.5" />{d('使用', 'Use')}</Button>
        </div>
      )}
    </div>
  );
}

/** The generator's trigger and popup. Icon only in a field row; in a single value field it reads
 *  「生成随机密码」 while the field has focus, and on a phone while the field is empty. */
export function GenerateControl({ tier, gen, open = false, suggested = false, labelled = false, empty = true }: {
  tier: Tier; gen: Gen; open?: boolean; suggested?: boolean; labelled?: boolean; empty?: boolean;
}) {
  const d = useDraft();
  const label = d('生成随机密码', 'Generate password');
  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={noop}
      modal
      align="end"
      sideOffset={8}
      sheetTitle={label}
      className="w-[340px] p-0"
      trigger={
        <button type="button" aria-label={label}
          className={cn('flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-[12px] font-medium text-mint-ink transition-colors hover:bg-mint-soft',
            open && 'bg-mint-soft')}>
          <Dices className="size-4" />
          {labelled ? <span className={cn('hidden group-focus-within/gen:inline', empty && 'max-md:inline', open && 'inline')}>{label}</span> : null}
        </button>
      }
    >
      <GeneratorPanel tier={tier} gen={gen} suggested={suggested} />
    </ResponsiveMenu>
  );
}

/** The single value with the generator attached: empty, generated (masked, chip), or Protected
 *  (no value; what will be generated, in words). */
export type FieldState = { kind: 'empty' } | { kind: 'generated'; gen: Gen } | { kind: 'protected'; gen: Gen };

export function GenValueField({ tier, state, open = false, suggested = false, gen }: { tier: Tier; state: FieldState; open?: boolean; suggested?: boolean; gen: Gen }) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <div className="flex flex-col gap-2">
      {tier === 'standard' ? (
        <div className="self-start">
          <SegmentedRadio<string> value="text" onChange={noop} ariaLabel={t('vaults.dialog.valueSource')}
            options={[{ id: 'text', label: t('vaults.dialog.valueSourceText') }, { id: 'file', label: t('vaults.dialog.valueSourceFile') }]} />
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <div className="group/gen relative min-w-0 flex-1">
          {state.kind === 'protected' ? (
            <span tabIndex={0} className="flex h-10 items-center gap-2 rounded-md border border-mint/40 bg-mint-soft/40 pl-3 pr-12 text-[12.5px] text-foreground">
              <Lock className="size-3.5 shrink-0 text-mint-ink" />
              <span className="truncate">{d('保存时在安全窗口里生成 · ', 'Generated in the secure window on save · ')}<GenSummary gen={state.gen} /></span>
            </span>
          ) : (
            <Input type="password" readOnly value={state.kind === 'generated' ? sampleFor(state.gen) : ''}
              placeholder={tier === 'protected' ? d('输入值', 'Type the value') : t('vaults.dialog.valuePlaceholder')}
              aria-label={t('vaults.dialog.value')}
              className={cn('pr-12 font-mono text-xs md:group-focus-within/gen:pr-36', state.kind === 'generated' && 'border-mint/40 bg-mint-soft/40', state.kind === 'empty' && 'max-md:pr-36')} />
          )}
          <div className="absolute inset-y-0 right-1 flex items-center">
            <GenerateControl tier={tier} gen={gen} open={open} suggested={suggested} labelled empty={state.kind === 'empty'} />
          </div>
        </div>
        {tier === 'standard' ? <Button type="button" variant="ghost" size="icon" aria-label={t('vaults.dialog.showValue')}><Eye className="size-4" /></Button> : null}
      </div>
      {state.kind === 'generated' ? (
        <div className="flex flex-wrap items-center gap-2">
          <GeneratedBadge />
          <span className="text-[11px] text-muted">{d('点「创建」才保存；直接输入就换成你自己的值。', 'Saved only when you save the secret; type to use your own instead.')}</span>
        </div>
      ) : state.kind === 'protected' ? (
        <span className="text-[11px] text-muted">{d('保存后可以用「显示值」在安全窗口里查看。', 'After saving, use Show value to see it in the secure window.')}</span>
      ) : null}
    </div>
  );
}

function GeneratedBadge() {
  const d = useDraft();
  return <Badge variant="success" className="shrink-0"><Sparkles className="size-3" />{d('已生成 · 未保存', 'Generated · not saved')}</Badge>;
}

function KindAndName({ name }: { name: string }) {
  const { t } = useTranslation();
  return (
    <>
      <div className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio<string> value="static" onChange={noop} ariaLabel={t('vaults.dialog.kindLabel')}
          options={[{ id: 'static', label: t('vaults.dialog.kindStatic'), icon: KeyRound }, { id: 'keypair', label: t('vaults.dialog.kindKeypair'), icon: Signature }]} />
      </div>
      <label className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.name')}</span>
        <Input readOnly value={name} className="font-mono" />
        <span className="text-[11.5px] text-muted">{t('vaults.dialog.nameHint')}</span>
      </label>
    </>
  );
}

/** Add a secret, static, one value: every field of the shipped create form. */
export function CreateDialog2({ tier, state, open = false, gen = PASSWORD_24, name = 'DB_PASSWORD' }: { tier: Tier; state: FieldState; open?: boolean; gen?: Gen; name?: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="create" />
      <KindAndName name={name} />
      <div className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.value')}</span>
        <GenValueField tier={tier} state={state} open={open} gen={gen} />
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

/** Add a secret with two fields; the generator is on each row, used here on password. */
export function CreateFieldsDialog2({ tier, open = false, used = false }: { tier: Tier; open?: boolean; used?: boolean }) {
  const { t } = useTranslation();
  const d = useDraft();
  const passwordValue = used && tier === 'standard' ? SAMPLE.password : '';
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="create" />
      <KindAndName name="APP_ADMIN" />
      <div className="flex flex-col gap-2">
        <VaultFieldsHeader count={2} />
        <VaultFieldList onAdd={noop}>
          <VaultFieldRow field="username" isDefault={false}
            name={{ editable: true, value: 'username', label: t('vaults.fields.name', { index: 1 }), onChange: noop }}
            value={{ value: 'admin', label: t('vaults.fields.value', { field: 'username' }), placeholder: t('vaults.fields.value', { field: 'username' }), onChange: noop }}
            action={<GenerateControl tier={tier} gen={PASSWORD_24} />} onSetDefault={noop} />
          <VaultFieldRow field="password" isDefault
            name={{ editable: true, value: 'password', label: t('vaults.fields.name', { index: 2 }), onChange: noop }}
            value={{ value: passwordValue, label: t('vaults.fields.value', { field: 'password' }),
              placeholder: used && tier === 'protected' ? d('保存时生成', 'Generated on save') : t('vaults.fields.value', { field: 'password' }), onChange: noop }}
            action={<GenerateControl tier={tier} gen={PASSWORD_24} open={open} />} />
          {used ? (
            <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
              {tier === 'standard' ? <GeneratedBadge /> : <Badge variant="success"><Lock className="size-3" />{d('保存时在安全窗口里生成', 'Generated in the secure window on save')}</Badge>}
              <span className="text-[11px] text-muted"><GenSummary gen={PASSWORD_24} />{d(' · 只有 password 这一行', ' · the password row only')}</span>
            </div>
          ) : null}
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

/** Edit a secret: a new value for one field, applied with that row's Save. */
export function EditFieldDialog2({ tier, open = false, used = false, provenance }: { tier: Tier; open?: boolean; used?: boolean; provenance?: ReactNode }) {
  const { t } = useTranslation();
  const d = useDraft();
  const filled = used && tier === 'standard';
  // With a provenance line this is the Agent-generated SESSION_SECRET, one field; otherwise APP_ADMIN.
  const agent = Boolean(provenance);
  const name = agent ? 'SESSION_SECRET' : 'APP_ADMIN';
  const fields = agent ? ['value'] : ['username', 'password'];
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="edit" />
      <div className="flex items-center gap-3 rounded-xl border border-edge bg-surface-2 p-3.5 shadow-elevation-0">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted/10 text-muted"><Lock className="size-4" /></span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="truncate font-mono text-[15px] font-semibold text-foreground">{name}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="secondary">{t('vaults.dialog.kindStatic')}</Badge>
            <Badge variant={tier === 'protected' ? 'warning' : 'secondary'}>{tier === 'protected' ? t('vaults.protected') : t('vaults.standard')}</Badge>
          </div>
        </div>
      </div>
      {provenance}
      <p className="flex items-center gap-1.5 text-[11px] text-muted"><Lock className="size-3 shrink-0" />{t('vaults.edit.lockedHint')}</p>
      <div className="flex flex-col gap-2">
        <VaultFieldsHeader count={fields.length} note={t('vaults.fields.editHelp')} />
        <VaultFieldList onAdd={noop}>
          {agent ? null : (
            <VaultFieldRow field="username" isDefault={false} name={{ editable: false, title: t('vaults.fields.extraNotRenamable') }}
              value={{ value: '', label: t('vaults.fields.value', { field: 'username' }), placeholder: t('vaults.fields.replacePlaceholder'), onChange: noop }}
              action={<GenerateControl tier={tier} gen={PASSWORD_24} />} />
          )}
          <VaultFieldRow field={fields[fields.length - 1]} isDefault name={{ editable: true, value: fields[fields.length - 1], label: t('vaults.fields.defaultName'), onChange: noop }}
            value={{ value: filled ? SAMPLE.password2 : '', label: t('vaults.fields.value', { field: fields[fields.length - 1] }),
              placeholder: used ? d('保存时生成', 'Generated on save') : t('vaults.fields.replacePlaceholder'), onChange: noop }}
            action={<><GenerateControl tier={tier} gen={agent ? HEX32 : PASSWORD_24} open={open} />{used ? <Button type="button" size="xs">{t('vaults.fields.save')}</Button> : null}</>} />
          {used ? (
            <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
              {tier === 'standard' ? <GeneratedBadge /> : <Badge variant="success"><Lock className="size-3" />{d('保存时在安全窗口里生成', 'Generated in the secure window on save')}</Badge>}
              <span className="text-[11px] text-muted">{d('点这一行的「保存」才替换；取消或关闭，已保存的值不变。', 'It replaces the saved value only when you click this row’s Save; cancel or close and the saved value stays.')}</span>
            </div>
          ) : null}
        </VaultFieldList>
        <VaultFieldReferences secretName={name} fields={fields} defaultField={fields[fields.length - 1]} />
      </div>
      <Description value={agent ? d('Web 会话签名密钥', 'Web session signing key') : d('后台管理员账号', 'Admin console account')} />
      <Tags values={agent ? ['web'] : ['prod']} />
      <Advanced />
      <Footer primary={t('vaults.dialog.save')} />
    </div>
  );
}

/** The Agent's request: its format suggestion presets the generator; the user still saves. */
export function ProvideDialog2({ tier, state, open = false }: { tier: Tier; state: FieldState; open?: boolean }) {
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
      <div className="flex items-start gap-2.5 rounded-[10px] border border-violet/30 bg-violet-soft px-3 py-2.5 text-[12px] leading-snug text-foreground">
        <WandSparkles className="mt-0.5 size-3.5 shrink-0 text-violet-ink" />
        <span className="flex min-w-0 flex-col gap-0.5">
          <span><span className="font-semibold">{d('Agent 建议生成：', 'The Agent suggests generating: ')}</span><GenSummary gen={HEX32} /></span>
          <span className="text-muted">{d('点值框里的骰子即按它的建议生成；它只说明了格式，拿不到这个值。', 'The dice in the value field starts from this; it only described the format and never gets the value.')}</span>
        </span>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.value')}</span>
        <GenValueField tier={tier} state={state} open={open} suggested gen={HEX32} />
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

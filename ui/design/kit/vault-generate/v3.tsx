// Version 3 of the generate-secret proposal (avibe#3338; owner 2026-11-14 00:27, after 1Password):
// the popup is live. Clicking 「随机密码」 puts a value straight into the field and opens the
// settings beside it; every change regenerates and the field, shown in clear while the popup is
// open, updates at once. No 使用 / 取消: closing the popup keeps what is in the field, masked again.
// The same anchored popup on a phone (no bottom sheet), so the field stays in view. Rows have no
// Save of their own: the dialog's Save commits every field. Draft copy; nothing here ships.
import type { ReactNode } from 'react';
import { Asterisk, Check, Dices, Eye, KeyRound, Lock, RefreshCw, Signature, Sparkles, WandSparkles, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Switch } from '@/components/ui/switch';
import { VaultFieldList, VaultFieldReferences, VaultFieldRow, VaultFieldsHeader } from '@/components/ui/vault-field-list';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { Slider, type SliderState } from '../slider/slider';
import { HEX32, PASSWORD_24, SAMPLE, SAMPLE_40, SAMPLE_BASE64, SYMBOLS, genBits, genChars, type Gen, type GenType } from './data';
import { AddFieldsLine, Advanced, Description, Footer, Header, Tags, TierCards, type Tier } from './dialogs';

const noop = () => {};
const LABEL = 'text-[13px] font-medium text-foreground';

const valueOf = (g: Gen) =>
  g.type === 'password' ? (g.length === 40 ? SAMPLE_40 : SAMPLE.password) : g.type === 'hex' ? SAMPLE.hex : SAMPLE_BASE64;

/** The control's name: what it will make. 「随机密码」, or 「随机密钥」 once the type is Hex/Base64
 *  (an Agent's suggestion, or the user's last choice in this field). */
function useGenLabel(g: Gen) {
  const d = useDraft();
  return g.type === 'password' ? d('随机密码', 'Random password') : d('随机密钥', 'Random key');
}

function GenSummary({ gen }: { gen: Gen }) {
  const d = useDraft();
  if (gen.type === 'password') return <>{d(`密码 · ${gen.length} 个字符`, `Password · ${gen.length} characters`)}</>;
  const name = gen.type === 'hex' ? 'Hex' : 'Base64';
  return <>{d(`${name} · ${gen.bytes} 字节`, `${name} · ${gen.bytes} bytes`)}</>;
}

function Row({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-10 items-center gap-3 border-t border-border py-1 md:min-h-11 md:py-1.5">
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-[13px] text-foreground">{label}</span>
        {hint ? <span className="text-[11px] text-muted">{hint}</span> : null}
      </span>
      {children}
    </div>
  );
}

/** The popup: settings only. The value is in the field above it. */
export function LivePanel({ tier, gen, slider = 'idle', suggested = false }: { tier: Tier; gen: Gen; slider?: SliderState; suggested?: boolean }) {
  const d = useDraft();
  const [min, max, value] = gen.type === 'password' ? [12, 128, gen.length] : [16, 64, gen.bytes];
  return (
    <div className="flex flex-col p-3">
      <div className="flex items-center gap-2 pb-2">
        {tier === 'standard' ? (
          <>
            <span className="flex-1 text-[11.5px] text-muted">{d(`约 ${genBits(gen)} 位随机性 · 改任何设置都会立刻换一个`, `About ${genBits(gen)} bits · any change makes a new one at once`)}</span>
            <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={d('换一个', 'Regenerate')}><RefreshCw className="size-4" /></Button>
          </>
        ) : (
          <span className="flex flex-1 items-start gap-1.5 text-[11.5px] leading-snug text-muted">
            <Lock className="mt-0.5 size-3.5 shrink-0 text-mint-ink" />
            {d('保护档：保存时在安全窗口里按这些设置生成，这里没有预览。', 'Protected: generated in the secure window on save with these settings; no preview here.')}
          </span>
        )}
      </div>
      {suggested ? (
        <span className="mb-2 flex items-center gap-1.5 rounded-md bg-violet-soft px-2.5 py-1.5 text-[11.5px] text-violet-ink">
          <WandSparkles className="size-3.5 shrink-0" />{d('按 Agent 的建议预设', 'Preset from the Agent’s suggestion')}
        </span>
      ) : null}
      <Row label={d('类型', 'Type')}>
        <SegmentedRadio<GenType> value={gen.type} onChange={noop} ariaLabel={d('类型', 'Type')} className="h-8"
          options={[{ id: 'password', label: d('密码', 'Password') }, { id: 'hex', label: 'Hex' }, { id: 'base64', label: 'Base64' }]} />
      </Row>
      <div className="flex flex-col border-t border-border pt-2">
        <div className="flex items-center gap-2">
          <span className="flex-1 text-[13px] text-foreground">{d('长度', 'Length')}</span>
          <Input type="number" readOnly value={value} aria-label={d('长度', 'Length')} className="h-8 w-16 px-2 text-center font-mono text-xs" />
          <span className="min-w-[4.5rem] text-[12px] text-muted">{gen.type === 'password' ? d('个字符', 'characters') : d(`字节 · ${genChars(gen)} 个字符`, `bytes · ${genChars(gen)} chars`)}</span>
        </div>
        <Slider value={value} min={min} max={max} label={d('长度', 'Length')} state={slider} />
      </div>
      {gen.type === 'password' ? (
        <>
          <Row label={d('数字', 'Digits')}><Switch checked={gen.digits} onCheckedChange={noop} label={d('数字', 'Digits')} /></Row>
          <Row label={d('符号', 'Symbols')} hint={<span className="font-mono">{SYMBOLS}</span>}><Switch checked={gen.symbols} onCheckedChange={noop} label={d('符号', 'Symbols')} /></Row>
          <Row label={d('排除易混字符', 'No look-alikes')} hint={d('不用 0 O 1 l I', 'Leaves out 0 O 1 l I')}><Switch checked={gen.noLookalike} onCheckedChange={noop} label={d('排除易混字符', 'No look-alikes')} /></Row>
        </>
      ) : gen.type === 'base64' ? (
        <span className="border-t border-border pt-2 text-[11px] text-muted">
          {d('标准 Base64（含 + / =），和 ', 'Standard Base64 (with + / =), the same as ')}<code className="whitespace-nowrap font-mono">openssl rand -base64</code>{d(' 输出的一样。', ' prints.')}
        </span>
      ) : null}
    </div>
  );
}

/** The popup, anchored to whatever `anchor` wraps (the field). */
function LivePopover({ open, anchor, children, inline = false }: { open: boolean; anchor: ReactNode; children: ReactNode; inline?: boolean }) {
  // `inline` draws the popup where the product's anchored popover sits, under the field, without
  // the portal: a phone frame's sheet is still settling when the popover would measure it.
  if (inline) {
    return (
      <div className="relative min-w-0 flex-1">
        {anchor}
        {open ? (
          <div className="absolute left-0 top-full z-50 mt-1.5 w-[calc(100%+3rem)] rounded-md border border-edge bg-popover text-text shadow-elevation-2">{children}</div>
        ) : null}
      </div>
    );
  }
  // Anchored to the field, so the popup lines up with it on desktop and phone alike; the control
  // inside the field opens it (in the product, a PopoverTrigger there).
  return (
    <Popover open={open} onOpenChange={noop} modal>
      <PopoverAnchor asChild>{anchor}</PopoverAnchor>
      <PopoverContent align="end" side="bottom" sideOffset={6} collisionPadding={12}
        className="w-[340px] max-w-[calc(100vw-1.5rem)] p-0" onOpenAutoFocus={(e) => e.preventDefault()}>
        {children}
      </PopoverContent>
    </Popover>
  );
}

function GenButton({ gen, open, labelled, empty }: { gen: Gen; open: boolean; labelled: boolean; empty: boolean }) {
  const label = useGenLabel(gen);
  return (
    <button type="button" aria-label={label} aria-expanded={open}
      className={cn('flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-[12px] font-medium text-mint-ink transition-colors hover:bg-mint-soft', open && 'bg-mint-soft')}>
      <Dices className="size-4" />
      {/* The name shows while the field has focus (always on a phone while empty); while the popup is
          open the field needs the width for the value, so the control is its icon. */}
      {labelled && !open ? <span className={cn('hidden group-focus-within/gen:inline', empty && 'max-md:inline')}>{label}</span> : null}
    </button>
  );
}

function GeneratedBadge() {
  const d = useDraft();
  return <Badge variant="success" className="shrink-0"><Sparkles className="size-3" />{d('已生成 · 未保存', 'Generated · not saved')}</Badge>;
}

export type Live =
  | { kind: 'empty' }
  | { kind: 'open'; gen: Gen; slider?: SliderState }
  | { kind: 'generated'; gen: Gen }
  | { kind: 'protected-open'; gen: Gen }
  | { kind: 'protected'; gen: Gen };

/** The single value field with the live generator. */
export function LiveValueField({ tier, state, suggested = false, preset = PASSWORD_24, inline = false }: { tier: Tier; state: Live; suggested?: boolean; preset?: Gen; inline?: boolean }) {
  const { t } = useTranslation();
  const d = useDraft();
  const gen = state.kind === 'empty' ? preset : state.gen;
  const open = state.kind === 'open' || state.kind === 'protected-open';
  const isProtected = state.kind === 'protected' || state.kind === 'protected-open';
  const field = (
    <div className="group/gen relative min-w-0 flex-1">
      {isProtected ? (
        <span tabIndex={0} className="flex h-10 items-center gap-2 rounded-md border border-mint/40 bg-mint-soft/40 pl-3 pr-12 text-[12.5px] text-foreground">
          <Lock className="size-3.5 shrink-0 text-mint-ink" />
          <span className="truncate">{d('保存时在安全窗口里生成 · ', 'Generated in the secure window on save · ')}<GenSummary gen={gen} /></span>
        </span>
      ) : (
        <Input type={open ? 'text' : 'password'} readOnly value={state.kind === 'empty' ? '' : valueOf(gen)}
          placeholder={tier === 'protected' ? d('输入值', 'Type the value') : t('vaults.dialog.valuePlaceholder')}
          aria-label={t('vaults.dialog.value')}
          className={cn('pr-12 font-mono text-xs md:group-focus-within/gen:pr-32', state.kind !== 'empty' && 'border-mint/40 bg-mint-soft/40', state.kind === 'empty' && 'max-md:pr-32', open && 'md:group-focus-within/gen:pr-12')} />
      )}
      <div className="absolute inset-y-0 right-1 flex items-center">
        <GenButton gen={gen} open={open} labelled empty={state.kind === 'empty'} />
      </div>
    </div>
  );
  return (
    <div className="flex flex-col gap-2">
      {tier === 'standard' ? (
        <div className="self-start">
          <SegmentedRadio<string> value="text" onChange={noop} ariaLabel={t('vaults.dialog.valueSource')}
            options={[{ id: 'text', label: t('vaults.dialog.valueSourceText') }, { id: 'file', label: t('vaults.dialog.valueSourceFile') }]} />
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <LivePopover open={open} anchor={field} inline={inline}>
          <LivePanel tier={tier} gen={gen} slider={state.kind === 'open' ? state.slider : undefined} suggested={suggested} />
        </LivePopover>
        {tier === 'standard' ? <Button type="button" variant="ghost" size="icon" aria-label={t('vaults.dialog.showValue')}><Eye className="size-4" /></Button> : null}
      </div>
      {/* While the popup is open it sits right under the field, over this line; the field's
          tint and clear text already say it is generated. */}
      {state.kind === 'generated' ? (
        <div className="flex flex-wrap items-center gap-2">
          <GeneratedBadge />
          <span className="text-[11px] text-muted">{d('保存时才存下；直接输入就换成你自己的值。', 'Kept only when you save; type to use your own instead.')}</span>
        </div>
      ) : state.kind === 'protected' ? (
        <span className="text-[11px] text-muted">{d('保存后可以用「显示值」在安全窗口里查看。', 'After saving, use Show value to see it in the secure window.')}</span>
      ) : null}
    </div>
  );
}

/** A field row with the live generator. Mirrors the shipped VaultFieldRow's layout (the build gives
 *  that row these states: in clear while the popup is open, the mint tint while generated). */
function LiveFieldRow({ field, isDefault, tier, state, editable = true }: { field: string; isDefault: boolean; tier: Tier; state: Live; editable?: boolean }) {
  const { t } = useTranslation();
  const d = useDraft();
  const gen = state.kind === 'empty' ? PASSWORD_24 : state.gen;
  const open = state.kind === 'open' || state.kind === 'protected-open';
  const isProtected = state.kind === 'protected' || state.kind === 'protected-open';
  const valueBox = (
    <div className="relative min-w-0 flex-1">
      {isProtected ? (
        <span className="flex h-10 items-center gap-1.5 rounded-md border border-mint/40 bg-mint-soft/40 pl-3 pr-2 text-[12px] text-foreground">
          <Lock className="size-3.5 shrink-0 text-mint-ink" /><span className="truncate">{d('保存时生成', 'Generated on save')}</span>
        </span>
      ) : (
        <Input type={open ? 'text' : 'password'} readOnly value={valueOf(gen)} aria-label={t('vaults.fields.value', { field })}
          className="border-mint/40 bg-mint-soft/40 pr-9 font-mono text-xs" />
      )}
      {isProtected ? null : (
        <Button type="button" variant="ghost" size="icon" aria-label={t('vaults.fields.showNamed', { field })}
          className="absolute right-1 top-1/2 size-7 -translate-y-1/2 text-muted hover:text-foreground"><Eye className="size-4" /></Button>
      )}
    </div>
  );
  return (
    <div className="group/field px-2.5 py-2">
      <div className="flex flex-col gap-1.5 @[25rem]/fields:flex-row @[25rem]/fields:items-center @[25rem]/fields:gap-2">
        <div className="flex min-w-0 items-center gap-1.5 @[25rem]/fields:w-[38%] @[25rem]/fields:shrink-0">
          {editable ? (
            <Input variant="bare" readOnly value={field} aria-label={t('vaults.fields.name', { index: 2 })}
              className="h-8 min-w-0 flex-1 rounded-md border border-transparent px-2 font-mono text-[13px]" />
          ) : <span className="min-w-0 flex-1 truncate px-2 font-mono text-[13px] leading-8 text-foreground">{field}</span>}
          {isDefault ? <Badge variant="success" className="shrink-0 px-2">{t('vaults.fields.default')}</Badge> : null}
        </div>
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <LivePopover open={open} anchor={valueBox}>
            <LivePanel tier={tier} gen={gen} />
          </LivePopover>
          <GenButton gen={gen} open={open} labelled={false} empty={false} />
          <span aria-hidden className="hidden size-7 shrink-0 @[25rem]/fields:block" />
        </div>
      </div>
      {state.kind === 'generated' ? (
        <div className="mt-1.5 flex items-center gap-2 px-2"><GeneratedBadge /></div>
      ) : null}
    </div>
  );
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

export function CreateDialog3({ tier, state, name = 'DB_PASSWORD', inline = false }: { tier: Tier; state: Live; name?: string; inline?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Header mode="create" />
      <KindAndName name={name} />
      <div className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.value')}</span>
        <LiveValueField tier={tier} state={state} inline={inline} />
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

export function CreateFieldsDialog3({ tier, state }: { tier: Tier; state: Live }) {
  const { t } = useTranslation();
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
            action={<GenButton gen={PASSWORD_24} open={false} labelled={false} empty />} onSetDefault={noop} />
          <LiveFieldRow field="password" isDefault tier={tier} state={state} />
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

/** Edit: no Save per row. Every row's draft, typed or generated, is kept by the dialog's Save. */
export function EditDialog3({ tier, state, provenance }: { tier: Tier; state: Live; provenance?: ReactNode }) {
  const { t } = useTranslation();
  const d = useDraft();
  const agent = Boolean(provenance);
  const name = agent ? 'SESSION_SECRET' : 'APP_ADMIN';
  const fields = agent ? ['value'] : ['username', 'password'];
  const last = fields[fields.length - 1];
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
        <VaultFieldsHeader count={fields.length} note={d('改完点下方「保存」一起生效 · 留空的保持原值', 'Changes apply together when you click Save below · blank keeps the saved value')} />
        <VaultFieldList onAdd={noop}>
          {agent ? null : (
            <VaultFieldRow field="username" isDefault={false} name={{ editable: false, title: t('vaults.fields.extraNotRenamable') }}
              value={{ value: '', label: t('vaults.fields.value', { field: 'username' }), placeholder: t('vaults.fields.replacePlaceholder'), onChange: noop }}
              action={<GenButton gen={PASSWORD_24} open={false} labelled={false} empty />} />
          )}
          {state.kind === 'empty' ? (
            <VaultFieldRow field={last} isDefault name={{ editable: true, value: last, label: t('vaults.fields.defaultName'), onChange: noop }}
              value={{ value: '', label: t('vaults.fields.value', { field: last }), placeholder: t('vaults.fields.replacePlaceholder'), onChange: noop }}
              action={<GenButton gen={agent ? HEX32 : PASSWORD_24} open={false} labelled={false} empty />} />
          ) : <LiveFieldRow field={last} isDefault tier={tier} state={state} />}
        </VaultFieldList>
        <VaultFieldReferences secretName={name} fields={fields} defaultField={last} />
      </div>
      <Description value={agent ? d('Web 会话签名密钥', 'Web session signing key') : d('后台管理员账号', 'Admin console account')} />
      <Tags values={agent ? ['web'] : ['prod']} />
      <Advanced />
      <Footer primary={t('vaults.dialog.save')} />
    </div>
  );
}

export function ProvideDialog3({ tier, state, inline = false }: { tier: Tier; state: Live; inline?: boolean }) {
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
          <span className="text-muted">{d('点值框里的「随机密钥」就按它的建议生成；它只说明了格式，拿不到这个值。', 'Random key in the value field starts from this; it only described the format and never gets the value.')}</span>
        </span>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className={LABEL}>{t('vaults.dialog.value')}</span>
        <LiveValueField tier={tier} state={state} suggested preset={HEX32} inline={inline} />
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

/** The slider on its own, large, in every state, for the kit review. */
export function SliderSheet() {
  const d = useDraft();
  const states: [SliderState, string][] = [
    ['idle', d('静止', 'Idle')], ['hover', d('悬停', 'Hover')], ['focus', d('键盘焦点', 'Keyboard focus')],
    ['pressed', d('按住拖动', 'Pressed / dragging')], ['disabled', d('不可用', 'Disabled')],
  ];
  return (
    <div className="flex flex-col gap-5 rounded-xl border border-edge bg-card p-6 shadow-elevation-0">
      <span className="text-[15px] font-semibold text-foreground">{d('滑动条 · 和开关、分段控件放在一起', 'Slider · beside the Switch and segmented control')}</span>
      <div className="flex flex-wrap items-center gap-4 rounded-lg bg-surface-2 p-4">
        <SegmentedRadio<string> value="a" onChange={noop} ariaLabel="demo" className="h-8" options={[{ id: 'a', label: d('密码', 'Password') }, { id: 'b', label: 'Hex' }, { id: 'c', label: 'Base64' }]} />
        <Switch checked onCheckedChange={noop} label="on" />
        <Switch checked={false} onCheckedChange={noop} label="off" />
        <div className="w-56"><Slider value={24} min={12} max={128} label="demo" /></div>
      </div>
      {states.map(([state, label]) => (
        <div key={state} className="grid grid-cols-[8rem_1fr] items-center gap-4">
          <span className="text-[13px] text-muted">{label}</span>
          <Slider value={state === 'pressed' ? 40 : 24} min={12} max={128} label={label} state={state} />
        </div>
      ))}
      <span className="text-[12px] leading-relaxed text-muted">
        {d('轨道 6px：未填充部分用开关关闭时的底色，填充部分用开关打开时的薄荷绿；拇指 20px，和开关的拇指同色同阴影。整行 44px 高，手指好按。键盘：←/→ 每次 1，Page Up/Down 每次 8，Home/End 到两端。',
          'Track 6px: the unfilled part is the Switch’s off rail, the filled part its mint on rail; the 20px thumb is the Switch’s thumb. The row is 44px tall for a finger. Keys: ←/→ by 1, Page Up/Down by 8, Home/End to the ends.')}
      </span>
    </div>
  );
}

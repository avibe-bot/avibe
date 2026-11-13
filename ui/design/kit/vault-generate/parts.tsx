// The generate-secret proposal (avibe#3338; owner brief 2026-11-13): one generator shared by the
// value field when creating a static secret, a field row when editing, and the Agent's request card.
// Draft copy (kit/draft.ts); nothing here is shipped.
import type { ReactNode } from 'react';
import { ChevronDown, ChevronRight, Dices, Eye, EyeOff, Lock, RefreshCw, Sparkles, WandSparkles } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { SYMBOLS, outputLength, type GenFormat, type Recipe } from './data';

const LABEL = 'text-[12px] font-medium text-foreground';

/** A checkbox with its label beside it, as a row the whole of which toggles. */
function Choice({ checked, label }: { checked: boolean; label: ReactNode }) {
  return (
    <span role="checkbox" aria-checked={checked} className="flex items-center gap-1.5 text-[12px] text-foreground">
      <Checkbox checked={checked} presentational />
      <span>{label}</span>
    </span>
  );
}

/** "Generated · not saved": marks a value this dialog made, until it is saved or typed over. */
export function GeneratedChip() {
  const d = useDraft();
  return <Badge variant="success" className="shrink-0"><Sparkles className="size-3" />{d('已生成 · 未保存', 'Generated · not saved')}</Badge>;
}

/** One line saying what the recipe makes, e.g. "Hex · 32 random bytes = 64 characters". */
export function RecipeSummary({ recipe }: { recipe: Recipe }) {
  const d = useDraft();
  if (recipe.format === 'password') {
    const sets = [recipe.lower && d('小写', 'lowercase'), recipe.upper && d('大写', 'uppercase'), recipe.digits && d('数字', 'digits'), recipe.symbols && d('符号', 'symbols')].filter(Boolean).join(d('、', ', '));
    return <>{d(`密码 · ${recipe.length} 个字符 · ${sets}`, `Password · ${recipe.length} characters · ${sets}`)}{recipe.noLookalike ? d(' · 不含易混字符', ' · no look-alikes') : ''}</>;
  }
  const name = recipe.format === 'hex' ? 'Hex' : 'Base64url';
  return <>{d(`${name} · ${recipe.bytes} 个随机字节 = ${outputLength(recipe)} 个字符`, `${name} · ${recipe.bytes} random bytes = ${outputLength(recipe)} characters`)}</>;
}

/** The options: format, then length and characters for a password, or random bytes for hex/base64url. */
export function GeneratorOptions({ recipe, compact = false }: { recipe: Recipe; compact?: boolean }) {
  const d = useDraft();
  const noop = () => {};
  return (
    <div className={cn('flex flex-col gap-2.5', compact && 'gap-2')}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(LABEL, 'w-12 shrink-0')}>{d('格式', 'Format')}</span>
        <SegmentedRadio<GenFormat>
          value={recipe.format}
          onChange={noop}
          ariaLabel={d('格式', 'Format')}
          options={[
            { id: 'password', label: d('密码', 'Password') },
            { id: 'hex', label: 'Hex' },
            { id: 'base64url', label: 'Base64url' },
          ]}
          className="h-8"
        />
      </div>
      {recipe.format === 'password' ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn(LABEL, 'w-12 shrink-0')}>{d('长度', 'Length')}</span>
            <Input type="number" readOnly value={recipe.length} aria-label={d('长度', 'Length')} className="h-8 w-20 font-mono text-xs" />
            <span className="text-[11.5px] text-muted">{d('个字符（12–128）', 'characters (12–128)')}</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pl-14">
            <Choice checked={recipe.lower} label={d('小写 a–z', 'Lowercase a–z')} />
            <Choice checked={recipe.upper} label={d('大写 A–Z', 'Uppercase A–Z')} />
            <Choice checked={recipe.digits} label={d('数字 0–9', 'Digits 0–9')} />
            <Choice checked={recipe.symbols} label={<>{d('符号', 'Symbols')} <span className="font-mono text-muted">{SYMBOLS}</span></>} />
            <Choice checked={recipe.noLookalike} label={d('排除易混字符（0 O 1 l I）', 'Exclude look-alikes (0 O 1 l I)')} />
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn(LABEL, 'w-12 shrink-0')}>{d('长度', 'Length')}</span>
          <Input type="number" readOnly value={recipe.bytes} aria-label={d('随机字节', 'Random bytes')} className="h-8 w-20 font-mono text-xs" />
          <span className="text-[11.5px] text-muted">
            {d(`个随机字节（16–64）= ${outputLength(recipe)} 个字符`, `random bytes (16–64) = ${outputLength(recipe)} characters`)}
            {recipe.format === 'base64url' ? d('，不带 =', ', no padding') : ''}
          </span>
        </div>
      )}
    </div>
  );
}

/** A generated value in the field: masked by default with the shipped eye, the chip, and Regenerate. */
export function GeneratedValue({ value, shown = false, regenerate = true }: { value: string; shown?: boolean; regenerate?: boolean }) {
  const d = useDraft();
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Input type={shown ? 'text' : 'password'} readOnly value={value} aria-label={d('生成的值', 'Generated value')}
          className="min-w-0 flex-1 border-mint/40 bg-mint-soft/40 font-mono text-xs" />
        <Button type="button" variant="ghost" size="icon" aria-label={shown ? d('隐藏', 'Hide') : d('显示', 'Show')}>
          {shown ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <GeneratedChip />
        {regenerate ? (
          <Button type="button" variant="ghost" size="xs" className="text-muted hover:text-foreground"><RefreshCw className="size-3.5" />{d('重新生成', 'Regenerate')}</Button>
        ) : null}
        <span className="text-[11px] text-muted">{d('保存前可以改选项或自己输入；取消不会动已保存的值。', 'Change the options or type your own before saving; cancelling leaves saved values alone.')}</span>
      </div>
    </div>
  );
}

/** Protected tier: nothing is generated in this page. The value is made inside the secure window
 *  when the secret is saved, from these options, and can be viewed there afterwards. */
export function ProtectedGenerateNote() {
  const d = useDraft();
  return (
    <div role="note" className="flex items-start gap-2.5 rounded-[10px] border border-mint/30 bg-mint/[0.06] px-3 py-2.5 text-[12px] leading-snug text-foreground">
      <Lock className="mt-0.5 size-3.5 shrink-0 text-mint-ink" />
      <span>
        <span className="font-semibold">{d('保存时在安全窗口里生成。', 'Generated in the secure window when you save.')}</span>{' '}
        {d('这个页面和 Agent 都看不到这个值；保存后可以用「显示值」在安全窗口里查看。', 'Neither this page nor any Agent sees the value; after saving, use Show value to see it in the secure window.')}
      </span>
    </div>
  );
}

/** Standard→protected with a value already generated here: it is dropped, never handed over. */
export function TierSwitchedNote() {
  const d = useDraft();
  return (
    <span role="status" className="text-[11.5px] text-muted">
      {d('已切到保护档：在这个页面生成的值已丢弃，保存时会在安全窗口里重新生成。', 'Switched to Protected: the value generated in this page was dropped; it will be generated again in the secure window when you save.')}
    </span>
  );
}

/** The Agent's recipe on a request: what it asked for, and that it gets no value from it. */
export function RecipeBanner({ recipe }: { recipe: Recipe }) {
  const d = useDraft();
  return (
    <div className="flex items-start gap-2.5 rounded-[10px] border border-violet/30 bg-violet-soft px-3 py-2.5 text-[12px] leading-snug text-foreground">
      <WandSparkles className="mt-0.5 size-3.5 shrink-0 text-violet-ink" />
      <span className="flex min-w-0 flex-col gap-0.5">
        <span><span className="font-semibold">{d('Agent 建议生成：', 'The Agent suggests generating: ')}</span><RecipeSummary recipe={recipe} /></span>
        <span className="text-muted">{d('它只说明了格式，拿不到这个值。可以改选项，也可以改成自己输入。', 'It only described the format and never gets the value. Change the options or type your own.')}</span>
      </span>
    </div>
  );
}

/** Variant B's trigger: a dice button inside the value row, beside the eye. */
export function DiceButton({ active = false }: { active?: boolean }) {
  const d = useDraft();
  return (
    <Button type="button" variant="ghost" size="icon" aria-label={d('生成', 'Generate')} className={active ? 'text-mint-ink' : undefined}>
      <Dices className="size-4" />
    </Button>
  );
}

/** Variant B's options line under a generated value: the recipe in words, folded; "Adjust" opens it. */
export function OptionsLine({ recipe, open = false }: { recipe: Recipe; open?: boolean }) {
  const d = useDraft();
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="flex flex-col gap-2 rounded-[10px] bg-surface-2 px-3 py-2">
      <button type="button" aria-expanded={open} className="flex items-center gap-1.5 text-left text-[11.5px] text-muted">
        <Chevron className="size-3.5" />
        <span className="flex-1"><RecipeSummary recipe={recipe} /></span>
        <span className="text-foreground">{open ? d('收起', 'Done') : d('调整', 'Adjust')}</span>
      </button>
      {open ? <GeneratorOptions recipe={recipe} compact /> : null}
    </div>
  );
}

/** A labelled block, as the Add dialog draws its sections. */
export function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[13px] font-medium text-foreground">{label}</span>
      {children}
    </div>
  );
}

import * as React from 'react';
import { Dices, RefreshCw, Sparkles, WandSparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { useIsMobile } from '@/lib/useIsMobile';
import { cn } from '@/lib/utils';
import {
  VAULT_GENERATE_SYMBOLS,
  defaultRecipe,
  generateSecretValue,
  outputLength,
  recipeBits,
  recipeSize,
  sizeLimits,
  withSize,
  type GenerateRecipe,
  type GenerateType,
} from '@/lib/vaultGenerate';

/** "Generated · not saved": a value this dialog made, until it is saved or typed over. */
export function VaultGeneratedBadge({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <Badge variant="success" className={cn('shrink-0', className)}>
      <Sparkles className="size-3" />
      {t('vaults.generate.generated')}
    </Badge>
  );
}

function Row({ label, hint, children }: { label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode }) {
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

/** The length, exact by keyboard: a number box that applies whole numbers within the limits and
 *  snaps back to the current size when left out of range. */
function SizeBox({ recipe, onChange }: { recipe: GenerateRecipe; onChange: (next: GenerateRecipe) => void }) {
  const { t } = useTranslation();
  const size = recipeSize(recipe);
  const { min, max } = sizeLimits(recipe.type);
  // Keyed by the size (see the caller), so a slider move starts it afresh with the new number.
  const [text, setText] = React.useState(String(size));
  const parsed = Number(text);
  const inRange = /^\d+$/.test(text) && parsed >= min && parsed <= max;
  return (
    <Input
      inputMode="numeric"
      value={text}
      aria-label={t('vaults.generate.length')}
      aria-invalid={!inRange || undefined}
      onChange={(event) => {
        const next = event.target.value.replace(/[^\d]/g, '').slice(0, 3);
        setText(next);
        const n = Number(next);
        if (/^\d+$/.test(next) && n >= min && n <= max && n !== size) onChange(withSize(recipe, n));
      }}
      onBlur={() => setText(String(size))}
      className={cn('h-8 w-16 px-2 text-center font-mono text-xs', !inRange && 'border-destructive/50')}
    />
  );
}

/** The popup's settings. Every change goes straight to `onChange`; the caller regenerates. */
export function VaultGeneratePanel({ recipe, onChange, onRegenerate, suggested = false }: {
  recipe: GenerateRecipe;
  onChange: (next: GenerateRecipe) => void;
  onRegenerate: () => void;
  /** The recipe came from the Agent's request. */
  suggested?: boolean;
}) {
  const { t } = useTranslation();
  const { min, max } = sizeLimits(recipe.type);
  const size = recipeSize(recipe);
  const sizeText = recipe.type === 'password'
    ? t('vaults.generate.chars', { count: size })
    : t('vaults.generate.bytes', { count: size, chars: outputLength(recipe) });
  return (
    <div className="flex flex-col p-3">
      <div className="flex items-center gap-2 pb-2">
        <span className="flex-1 text-[11.5px] text-muted">{t('vaults.generate.bits', { bits: recipeBits(recipe) })}</span>
        <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={t('vaults.generate.regenerate')} onClick={onRegenerate}>
          <RefreshCw className="size-4" />
        </Button>
      </div>
      {suggested ? (
        <span className="mb-2 flex items-center gap-1.5 rounded-md bg-violet-soft px-2.5 py-1.5 text-[11.5px] text-violet-ink">
          <WandSparkles className="size-3.5 shrink-0" />
          {t('vaults.generate.suggested')}
        </span>
      ) : null}
      <Row label={t('vaults.generate.type')}>
        <SegmentedRadio<GenerateType>
          value={recipe.type}
          onChange={(type) => {
            if (type !== recipe.type) onChange(defaultRecipe(type));
          }}
          ariaLabel={t('vaults.generate.type')}
          className="h-8"
          options={[
            { id: 'password', label: t('vaults.generate.typePassword') },
            { id: 'hex', label: t('vaults.generate.typeHex') },
            { id: 'base64', label: t('vaults.generate.typeBase64') },
          ]}
        />
      </Row>
      <div className="flex flex-col border-t border-border pt-2">
        <div className="flex items-center gap-2">
          <span className="flex-1 text-[13px] text-foreground">{t('vaults.generate.length')}</span>
          <SizeBox key={`${recipe.type}:${size}`} recipe={recipe} onChange={onChange} />
          <span className="min-w-[4.5rem] text-[12px] text-muted">
            {recipe.type === 'password' ? t('vaults.generate.unitChars') : t('vaults.generate.unitBytes', { chars: outputLength(recipe) })}
          </span>
        </div>
        <Slider
          value={size}
          min={min}
          max={max}
          label={t('vaults.generate.length')}
          valueText={sizeText}
          onValueChange={(next) => onChange(withSize(recipe, next))}
        />
      </div>
      {recipe.type === 'password' ? (
        <>
          <Row label={t('vaults.generate.digits')}>
            <Switch checked={recipe.digits} onCheckedChange={(digits) => onChange({ ...recipe, digits })} label={t('vaults.generate.digits')} />
          </Row>
          <Row label={t('vaults.generate.symbols')} hint={<span className="font-mono">{VAULT_GENERATE_SYMBOLS}</span>}>
            <Switch checked={recipe.symbols} onCheckedChange={(symbols) => onChange({ ...recipe, symbols })} label={t('vaults.generate.symbols')} />
          </Row>
          <Row label={t('vaults.generate.noLookalikes')} hint={t('vaults.generate.noLookalikesHint')}>
            <Switch checked={recipe.noLookalikes} onCheckedChange={(noLookalikes) => onChange({ ...recipe, noLookalikes })} label={t('vaults.generate.noLookalikes')} />
          </Row>
        </>
      ) : recipe.type === 'base64' ? (
        <span className="border-t border-border pt-2 text-[11px] text-muted">{t('vaults.generate.base64Note')}</span>
      ) : null}
    </div>
  );
}

/**
 * The generator attached to a value field (ui/design: vault/generate-secret). `children` is the
 * field; the popup anchors to it. Clicking the control puts a fresh value in the field at once and
 * opens the settings; every change regenerates. There is no Use or Cancel: closing the popup keeps
 * what is in the field, and nothing is saved until the dialog's own Save. Same on both tiers: a
 * protected value is sealed at Save like a typed one. The popup is modal inside the modal dialog
 * (its sliders and switches need the pointer), and on a phone it is the same anchored popup, with
 * the field scrolled into view, so the value stays visible while it changes.
 */
export function VaultGenerateControl({
  children,
  recipe,
  onRecipeChange,
  onValue,
  open,
  onOpenChange,
  labelled = false,
  showLabel = false,
  suggested = false,
  disabled = false,
  triggerOutside = false,
}: {
  children: React.ReactNode;
  recipe: GenerateRecipe;
  onRecipeChange: (next: GenerateRecipe) => void;
  /** A new value for the field. */
  onValue: (value: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Room for the control's name (a single value field); a field row shows only the icon. */
  labelled?: boolean;
  /** Show the name even without focus (an empty field on a phone). */
  showLabel?: boolean;
  suggested?: boolean;
  disabled?: boolean;
  /** Put the control beside the field instead of inside it (a field row, whose input has its eye). */
  triggerOutside?: boolean;
}) {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const anchorRef = React.useRef<HTMLDivElement>(null);
  const name = recipe.type === 'password' ? t('vaults.generate.password') : t('vaults.generate.key');
  const generate = (next: GenerateRecipe) => onValue(generateSecretValue(next));
  const trigger = (
    <PopoverTrigger asChild>
      <button
        type="button"
        disabled={disabled}
        aria-label={name}
        className={cn(
          'flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-[12px] font-medium text-mint-ink transition-colors hover:bg-mint-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50',
          open && 'bg-mint-soft',
        )}
      >
        <Dices className="size-4" />
        {labelled && !open ? (
          <span className={cn('hidden group-focus-within/gen:inline', showLabel && 'max-md:inline')}>{name}</span>
        ) : null}
      </button>
    </PopoverTrigger>
  );
  const change = (next: GenerateRecipe) => {
    onRecipeChange(next);
    generate(next);
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (next && !open) {
          if (isMobile) anchorRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
          generate(recipe);
        }
        onOpenChange(next);
      }}
      modal
    >
      {triggerOutside ? (
        // The trigger sits inside the anchor: Radix takes the trigger as the anchor when it registers
        // after a sibling custom anchor, and the popup would then lose its place.
        <PopoverAnchor asChild>
          <div ref={anchorRef} className="flex min-w-0 flex-1 scroll-mt-4 items-center gap-1.5">
            <div className="relative min-w-0 flex-1">{children}</div>
            {trigger}
          </div>
        </PopoverAnchor>
      ) : (
        <PopoverAnchor asChild>
          <div ref={anchorRef} className="group/gen relative min-w-0 flex-1 scroll-mt-4">
            {children}
            <div className="absolute inset-y-0 right-1 flex items-center">{trigger}</div>
          </div>
        </PopoverAnchor>
      )}
      <PopoverContent
        align="end"
        side="bottom"
        sideOffset={6}
        collisionPadding={12}
        className="w-[340px] max-w-[calc(100vw-1.5rem)] p-0 max-md:w-[var(--radix-popper-anchor-width)]"
      >
        <VaultGeneratePanel recipe={recipe} onChange={change} onRegenerate={() => generate(recipe)} suggested={suggested} />
      </PopoverContent>
    </Popover>
  );
}

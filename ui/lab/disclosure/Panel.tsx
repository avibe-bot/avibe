// The lab's floating controls: which expand variant, light or dark, reduced motion, slow motion, and the
// live events (a reply arriving, a turn running). Folded to a small tab on the right edge, out of the way
// of the header, the composer, and the transcript's taps.
import { createPortal } from 'react-dom';

import { type LabPrefs, type Speed, type Variant } from './prefs';

declare const __LAB_SOURCE__: { branch: string; head: string; built: string };

const VARIANTS: { id: Variant; name: string; hint: string }[] = [
  { id: 'a', name: 'A', hint: '现状：淡入 + 0.98 放大，160ms' },
  { id: 'b', name: 'B', hint: '更明显：淡入 + 下落 8px + 0.96 放大，220ms' },
  { id: 'c', name: 'C', hint: '展开：从上往下揭开 + 轻淡入，220ms' },
  { id: 'd', name: 'D', hint: 'C + 下面的行跟着往下让开；收起时跟着往上收' },
];

/** D's speeds: opening, and closing (the fold with its cross-fade into the summary), in ms (src/index.css). */
const SPEEDS: { id: Speed; name: string; open: number; close: number }[] = [
  { id: '1', name: '1×', open: 220, close: 180 },
  { id: '0.75', name: '0.75×', open: 290, close: 230 },
  { id: '0.5', name: '0.5×', open: 440, close: 350 },
];
const seconds = (ms: number) => `${(ms / 1000).toFixed(2).replace(/0$/, '')}s`;

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { id: T; name: string }[]; onChange: (value: T) => void }) {
  return (
    <div className="grid gap-1 rounded-lg bg-foreground/[0.06] p-0.5" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          onClick={() => onChange(option.id)}
          className={
            option.id === value
              ? 'rounded-md bg-background py-0.5 text-[12px] font-semibold text-foreground shadow-sm'
              : 'rounded-md py-0.5 text-[12px] text-muted'
          }
        >
          {option.name}
        </button>
      ))}
    </div>
  );
}

function Toggle({ label, on, onChange }: { label: string; on: boolean; onChange: (on: boolean) => void }) {
  return (
    <button type="button" onClick={() => onChange(!on)} className="flex w-full items-center justify-between py-0.5 text-[12px]">
      <span>{label}</span>
      <span className={`relative h-5 w-9 rounded-full transition-colors ${on ? 'bg-mint' : 'bg-foreground/15'}`}>
        <span className={`absolute top-0.5 size-4 rounded-full bg-white shadow transition-[left] ${on ? 'left-[18px]' : 'left-0.5'}`} />
      </span>
    </button>
  );
}

export function Panel({
  prefs,
  onChange,
  running,
  onReply,
  onRun,
  onSettle,
}: {
  prefs: LabPrefs;
  onChange: (next: Partial<LabPrefs>) => void;
  running: boolean;
  onReply: () => void;
  onRun: () => void;
  onSettle: () => void;
}) {
  const variant = VARIANTS.find((option) => option.id === prefs.variant) ?? VARIANTS[0];
  const speed = SPEEDS.find((option) => option.id === prefs.speed) ?? SPEEDS[2];
  const tab = `${variant.name}${prefs.variant === 'd' ? ` ${speed.name}` : ''}${prefs.slow ? ' · 慢放' : ''}${prefs.reduced ? ' · 减弱' : ''}`;
  return createPortal(
    <div
      className="fixed right-0 z-[1000] text-foreground"
      style={{ top: 'calc(env(safe-area-inset-top) + 22vh)' }}
    >
      {!prefs.open ? (
        <button
          type="button"
          onClick={() => onChange({ open: true })}
          className="rounded-l-full border border-r-0 border-border bg-background/90 py-1.5 pl-3 pr-2 text-[12px] font-semibold shadow-md backdrop-blur"
        >
          动效 {tab}
        </button>
      ) : (
        <div className="mr-2 w-[212px] rounded-2xl border border-border bg-background/95 p-2.5 text-[12px] shadow-xl backdrop-blur">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[12px] font-semibold">展开动效 Lab</span>
            <button type="button" onClick={() => onChange({ open: false })} className="rounded-md px-2 py-0.5 text-[12px] text-muted">
              收起
            </button>
          </div>
          <div className="mb-1 text-[11px] text-muted">展开方案</div>
          <Segmented value={prefs.variant} options={VARIANTS} onChange={(value) => onChange({ variant: value })} />
          <div className="mt-1 min-h-[28px] text-[10.5px] leading-snug text-muted">{variant.hint}</div>
          {prefs.variant === 'd' && (
            <>
              <div className="mt-1 mb-1 text-[11px] text-muted">D 的速度</div>
              <Segmented value={prefs.speed} options={SPEEDS} onChange={(value) => onChange({ speed: value })} />
              <div className="mt-1 text-[10.5px] text-muted">
                展开 {seconds(speed.open)} · 收起 {seconds(speed.close)}
                {prefs.slow ? '（慢放中再 ×4）' : ''}
              </div>
            </>
          )}
          <div className="mt-2 mb-1 text-[11px] text-muted">外观</div>
          <Segmented
            value={prefs.theme}
            options={[{ id: 'light', name: '亮色' }, { id: 'dark', name: '暗色' }]}
            onChange={(value) => onChange({ theme: value })}
          />
          <div className="mt-2 border-t border-border pt-1">
            <Toggle label="减弱动态效果" on={prefs.reduced} onChange={(on) => onChange({ reduced: on })} />
            <Toggle label="慢放 0.25×" on={prefs.slow} onChange={(on) => onChange({ slow: on })} />
          </div>
          <div className="mt-2 grid grid-cols-2 gap-1.5 border-t border-border pt-2">
            <button type="button" onClick={onReply} className="rounded-lg border border-border py-1 text-[11.5px] font-medium">
              来一条回复
            </button>
            {running ? (
              <button type="button" onClick={onSettle} className="rounded-lg border border-border py-1 text-[11.5px] font-medium">
                结束这一轮
              </button>
            ) : (
              <button type="button" onClick={onRun} className="rounded-lg border border-border py-1 text-[11.5px] font-medium">
                跑一轮
              </button>
            )}
          </div>
          <div className="mt-2 text-[10px] leading-snug text-muted">
            {__LAB_SOURCE__.branch} @ {__LAB_SOURCE__.head.slice(0, 9)} · {__LAB_SOURCE__.built}
          </div>
        </div>
      )}
    </div>,
    document.body,
  );
}

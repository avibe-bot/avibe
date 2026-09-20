import { Sparkles } from 'lucide-react';
import clsx from 'clsx';

// askill's registry AI score is on a 0–100 scale. Mint ≥90, cyan ≥85, gold
// below — and display mirrors askill's own formatScore (integer as-is, else
// one decimal), so a 96 shows "AI 96", not "AI 96.0".
function scoreChip(score: number): string {
  if (score >= 90) return 'bg-mint-soft border-mint/40 text-mint-ink';
  if (score >= 85) return 'bg-cyan-soft border-cyan/40 text-cyan-ink';
  return 'bg-gold/[0.12] border-gold/40 text-gold-ink';
}

/** Compact "AI 9.5" pill used on registry result cards. */
export function AiScoreBadge({ score, className }: { score: number | null | undefined; className?: string }) {
  // askill's aiScore can be null for unscored registry results — hide the badge
  // rather than throwing on score.toFixed (which blanks the whole modal).
  if (score == null || !Number.isFinite(score)) return null;
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 font-mono text-[10px] font-bold',
        scoreChip(score),
        className,
      )}
    >
      <Sparkles className="size-2.5" />
      AI {Number.isInteger(score) ? score : score.toFixed(1)}
    </span>
  );
}

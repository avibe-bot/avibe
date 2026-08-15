// The Badge class recipe, kept beside the component rather than inside it: the
// chips that only need the classes (a Link, a native <span> in a table cell)
// import the recipe, and a component module stays exporting only components.
import { cva } from 'class-variance-authority';

export const badgeVariants = cva(
  'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-semibold tracking-wide transition-colors focus:outline-none',
  {
    variants: {
      variant: {
        default: 'border border-border-strong bg-surface text-foreground',
        secondary: 'border border-border bg-surface text-muted',
        outline: 'border border-border-strong bg-transparent text-foreground',
        success: 'border border-mint/40 bg-mint-soft text-mint-ink',
        warning: 'border border-gold/40 bg-gold/10 text-gold-ink',
        info: 'border border-cyan/40 bg-cyan-soft text-cyan-ink',
        destructive: 'border border-destructive/40 bg-destructive/10 text-destructive-ink',
        recommendation:
          'max-w-[140px] shrink-0 whitespace-normal rounded-full border px-[7px] py-0.5 text-center text-[10px] font-semibold leading-[1.2] tracking-normal',
        // Eyebrow — JetBrains Mono cyan w/ glow (design.pen Badge/Eyebrow mtcmf).
        eyebrow:
          'rounded-full border border-cyan/50 bg-cyan/[0.16] px-3 py-1.5 font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-cyan-ink shadow-glow-md-cyan',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

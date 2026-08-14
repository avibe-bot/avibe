// The Button class recipe, kept beside the component rather than inside it: the
// call sites that need Button's look on a non-button element (an anchor, a Radix
// trigger) import the recipe, and a component module stays exporting only components.
import { cva } from 'class-variance-authority';

export const buttonVariants = cva(
  'inline-flex items-center justify-center whitespace-nowrap rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        // Mint primary — flat, no glow shadow (design.pen Button/Default).
        default: 'gap-1.5 bg-primary text-primary-foreground hover:bg-primary-hover',
        // Brand CTA — bright bg + brand-color glow shadow + bold text.
        // The fill is the accent itself and the label its paired *-foreground: a dark
        // ink on the dark theme's neon accents, white on light's vivid ones (and white
        // in both for violet, the one accent whose label does not follow the theme).
        // All are the design's own pairing; see the ACCEPTED_BRAND_PAIRS note in
        // ui/scripts/validate-theme.mjs for why light's white label is deliberate.
        //
        // Hover paints a declared --X-hover fill rather than filtering the button,
        // because brightness() scales the label along with the background: on light's
        // white labels that made hover strictly worse than the pairing the owner
        // accepted. A named fill also lets validate-theme.mjs measure the hovered pair
        // and assert it never reads worse than the resting one — which is how violet's
        // inverted direction surfaces at build time rather than in review. The glow
        // shadow no longer brightens with the fill; the fill swap carries the
        // affordance. See the --primary-hover note in index.css.
        //
        // Glow blur is 16px on all four accents (owner decision 2026-08-14: 24px read
        // too heavy). One radius, four accents — a per-accent radius would be the same
        // drift this variant exists to remove. design.pen has no glow on any Button, so
        // the value is ours to hold, not the design's; keep the four in lockstep.
        brand:
          'gap-2 bg-mint font-bold text-primary-foreground shadow-[0_0_16px_-4px_rgba(91,255,160,0.6)] hover:bg-mint-hover disabled:shadow-none',
        'brand-cyan':
          'gap-2 bg-cyan font-bold text-accent-foreground shadow-[0_0_16px_-4px_rgba(63,224,229,0.6)] hover:bg-cyan-hover disabled:shadow-none',
        'brand-gold':
          'gap-2 bg-gold font-bold text-gold-foreground shadow-[0_0_16px_-4px_rgba(255,200,87,0.55)] hover:bg-gold-hover disabled:shadow-none',
        'brand-violet':
          'gap-2 bg-violet font-bold text-violet-foreground shadow-[0_0_16px_-4px_rgba(124,91,255,0.55)] hover:bg-violet-hover disabled:shadow-none',
        secondary: 'gap-1.5 border border-border bg-secondary text-secondary-foreground hover:border-border-strong',
        // Outline — bg matches page surface so it sits cleanly on glow gradients.
        outline:
          'gap-1.5 border border-border bg-background text-foreground shadow-[0_1px_2px_rgba(0,0,0,0.05)] hover:bg-surface-2',
        // Cyan outline — for "Read Vibe Remote" / docs style CTAs.
        'outline-cyan':
          'gap-1.5 border border-cyan/40 bg-cyan/[0.06] text-cyan-ink hover:bg-cyan/[0.10]',
        ghost: 'gap-1.5 text-foreground hover:bg-surface-2',
        // Hover swaps the fill rather than fading it: opacity blends the fill toward
        // the page surface, which pulls the white label *down* toward AA (4.70:1 ->
        // ~4.25:1 in light). A fixed brightness-95 was no better — it only deepened,
        // which helps light's white label but drags dark's #080812 label the wrong
        // way. --destructive-hover is declared per theme like every other brand fill.
        destructive: 'gap-1.5 bg-destructive text-destructive-foreground hover:bg-destructive-hover',
        // Pink-soft destructive — design.pen T09T8Z. Pink fill + pink border
        // + pink text/icon, used for in-panel delete CTAs where a full
        // destructive shouts too loud. Drives the --pink / --pink-soft tokens
        // (see index.css); the old bg-[#FF5B8A14] one-off plus an unresolved
        // `pink` token left the border falling back to currentColor (black in
        // light, white in dark) and the fill near-invisible.
        // Fill is pink/15 (not the 10% --pink-soft token) so it carries the
        // same visual weight as the mint-soft "Run" button it usually sits next
        // to — a balanced soft pair rather than a near-invisible wash.
        'destructive-soft':
          'gap-1.5 border border-pink/45 bg-pink/15 text-pink-ink hover:border-pink/60 hover:bg-pink/[0.22]',
        link: 'text-primary-ink underline-offset-4 hover:underline',
        accent: 'gap-1.5 border border-cyan/40 bg-cyan-soft text-cyan-ink hover:bg-cyan/15',
      },
      size: {
        // h-8 toolbar buttons (LogsPanel/DoctorPanel/SettingsServicePage/AgentDetection toolbar).
        xs: 'h-8 px-3 text-[12px] [&_svg]:size-3.5',
        // h-9 config-inline CTAs (Slack/Discord/Telegram/...).
        sm: 'h-9 px-4 text-[13px] [&_svg]:size-3.5',
        // h-10 wizard "下一步" — most common.
        default: 'h-10 px-5 text-[13px] [&_svg]:size-3.5',
        // h-12 prominent CTAs (Summary main step CTA).
        lg: 'h-12 px-7 text-[14px] [&_svg]:size-4',
        // Welcome-only hero CTA.
        hero: 'h-[52px] rounded-xl px-8 text-[15px] [&_svg]:size-4',
        icon: 'h-9 w-9',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

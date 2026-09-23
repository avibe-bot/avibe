import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';

import { badgeVariants, inlineBadgeTriggerClassName } from './badge-variants';
import { Popover, PopoverAnchor, PopoverContent } from './popover';
import type { CitationSource } from '@/lib/citations';
import { cn } from '@/lib/utils';

// One numbered source badge for a citation in an agent answer.
//
// The badge IS the link, which is what keeps it usable without trapping focus:
// focus reveals the preview and Enter opens the page, so the portalled panel
// never has to be tabbed into, and its `aria-label` already carries the full
// attribution — the panel is what a sighted reader hovers for, not where the
// information only lives. (InfoHint can't be reused here for exactly that
// reason: its trigger is a <button>, so a link in its panel would be the only
// way out and keyboard-unreachable.)
//
// A mouse hovers to preview and clicks to open. Touch has no hover, so the FIRST
// tap reveals the preview instead of navigating and every later tap follows the
// link; the panel repeats the destination as an explicit action so the way out
// is never a guess. Non-modal on purpose — an inline citation sits in transcript
// prose, not inside a Dialog.
export const CitationBadge: React.FC<{ citation: CitationSource; className?: string }> = ({
  citation,
  className,
}) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const pointerType = React.useRef<string>('mouse');
  // Whether this badge has already shown its preview once. Deliberately a ref
  // and not the `open` state: an outside tap closes the panel on pointerdown
  // before the click lands, so a second tap reading `open` would re-open instead
  // of navigating and the page would be unreachable on a phone.
  const revealed = React.useRef(false);
  // Hover-close is deferred so the pointer can travel from the badge into the
  // panel — which is portalled, so no wrapper can hold both.
  const closeTimer = React.useRef<number | null>(null);
  const cancelClose = React.useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const scheduleClose = React.useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 120);
  }, [cancelClose]);
  React.useEffect(() => cancelClose, [cancelClose]);

  // A source whose search returned no title is attributed by its domain, which
  // is the one thing every citation has.
  const title = citation.title?.trim() || citation.label;
  // The title is whatever the search result said about itself; the domain is
  // where the link actually goes. A screen reader hears both, so the
  // destination is never something only the hover preview shows.
  const accessibleName = title === citation.label
    ? t('chat.citation.badge', { index: citation.index, source: title })
    : t('chat.citation.badgeWithDomain', {
        index: citation.index,
        source: title,
        domain: citation.label,
      });

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <a
          href={citation.url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          aria-label={accessibleName}
          data-citation-index={citation.index}
          onPointerDown={(event) => { pointerType.current = event.pointerType; }}
          // Only a mouse hovers; a tap reports pointerType 'touch' and is left to
          // the click handler, so a phone gets one reveal instead of open-then-close.
          onPointerEnter={(event) => {
            if (event.pointerType === 'mouse') { cancelClose(); setOpen(true); }
          }}
          onPointerLeave={(event) => {
            if (event.pointerType === 'mouse') scheduleClose();
          }}
          onFocus={() => { cancelClose(); setOpen(true); }}
          onBlur={scheduleClose}
          onClick={(event) => {
            if (pointerType.current === 'touch' && !revealed.current) {
              event.preventDefault();
              revealed.current = true;
              cancelClose();
              setOpen(true);
            }
          }}
          className={cn(
            badgeVariants({ variant: 'secondary' }),
            inlineBadgeTriggerClassName,
            // Pin the chip's own leading. It is baseline-aligned inline content, so
            // its box has to fit inside the prose line box or every line carrying a
            // citation grows taller than the uncited lines around it; inheriting
            // `.vr-markdown`'s 1.6 makes it do exactly that. (Same reason the
            // `recommendation` badge variant fixes its own leading.)
            'vr-citation align-baseline px-1.5 font-mono leading-none tabular-nums focus-visible:ring-1 focus-visible:ring-cyan/60',
            className,
          )}
        >
          {citation.index}
        </a>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        sideOffset={6}
        collisionPadding={12}
        // Don't steal focus from the badge on hover-open: the badge is what opens
        // the page, and moving focus into the portal would break tab order.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onPointerEnter={cancelClose}
        onPointerLeave={scheduleClose}
        className="w-64 space-y-1.5 p-3"
      >
        <p className="text-[12px] font-medium leading-snug text-foreground">{title}</p>
        <p className="truncate text-[11px] leading-snug text-muted">{citation.label}</p>
        <a
          href={citation.url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-cyan-ink hover:underline"
        >
          {t('chat.citation.open')}
          <ExternalLink className="size-3" aria-hidden="true" />
        </a>
      </PopoverContent>
    </Popover>
  );
};

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Share, Sparkles, X } from 'lucide-react';

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { Button } from './ui/button';
import { isRealMobileSafari, isStandalonePwa } from '@/lib/platform';

const STORAGE_KEY = 'vibe-remote-a2hs';

// Only real iOS Safari can "Add to Home Screen": IM in-app webviews and alt
// browsers (CriOS/FxiOS/etc.) don't expose Safari's share flow, so the nudge
// would give impossible steps to exactly the IM-launched users this app targets.
// isRealMobileSafari() (shared in lib/platform) encodes that filter.
function shouldShowHint(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  if (!window.matchMedia('(max-width: 767px)').matches) return false;
  if (!isRealMobileSafari()) return false;
  return !isStandalonePwa();
}

// Top-right nudge to install the app to the iOS Home Screen (standalone PWA),
// which removes Safari's chrome and the keyboard whitespace/accessory issues.
// The ✕ collapses it to a persistent gold dot (we keep nudging rather than
// dismissing for good); both the bar and the dot open a popover with the
// Share → Add to Home Screen steps. Mounted in both the brand header and the
// chat header (only one renders at a time) so chat-first / IM-launched users
// see it too. Self-gates to null off iOS Safari / when installed.
export const InstallHint: React.FC = () => {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setVisible(shouldShowHint());
    try {
      setCollapsed(window.localStorage.getItem(STORAGE_KEY) === 'dot');
    } catch {
      /* private mode / storage blocked — keep it expanded */
    }
  }, []);

  if (!visible) return null;

  const collapse = () => {
    setOpen(false);
    setCollapsed(true);
    try {
      window.localStorage.setItem(STORAGE_KEY, 'dot');
    } catch {
      /* ignore */
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      {collapsed ? (
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t('installHint.cta')}
            className="relative size-7 shrink-0 hover:bg-transparent"
          >
            <span className="size-2.5 rounded-full bg-gold shadow-[0_0_8px_rgba(245,200,92,0.9)]" />
            <span className="absolute inline-flex size-2.5 animate-ping rounded-full bg-gold/60" />
          </Button>
        </PopoverTrigger>
      ) : (
        <div className="inline-flex shrink-0 items-center gap-0.5">
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 gap-1 rounded-full border-gold/40 bg-gold/[0.12] px-2.5 text-[11px] font-semibold text-gold hover:bg-gold/20 hover:text-gold"
            >
              <Sparkles className="size-3" />
              <span>{t('installHint.cta')}</span>
            </Button>
          </PopoverTrigger>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={collapse}
            aria-label={t('installHint.dismiss')}
            className="size-6 shrink-0 text-gold/70 hover:bg-transparent hover:text-gold"
          >
            <X className="size-3.5" />
          </Button>
        </div>
      )}

      <PopoverContent align="end" sideOffset={8} className="w-[18rem] border-gold/30">
        <div className="flex flex-col gap-2.5">
          <div className="flex items-center gap-2">
            <span className="grid size-7 shrink-0 place-items-center rounded-lg border border-gold/40 bg-gold/[0.12] text-gold">
              <Sparkles className="size-3.5" />
            </span>
            <div className="text-[13px] font-semibold text-foreground">{t('installHint.title')}</div>
          </div>
          <p className="text-[12px] leading-relaxed text-muted">{t('installHint.body')}</p>
          <ol className="flex flex-col gap-1.5 rounded-lg border border-border bg-foreground/[0.02] p-2.5 text-[12px] text-foreground">
            <li className="flex items-center gap-2">
              <Share className="size-4 shrink-0 text-cyan" />
              <span>{t('installHint.step1')}</span>
            </li>
            <li className="flex items-center gap-2">
              <Plus className="size-4 shrink-0 text-cyan" />
              <span>{t('installHint.step2')}</span>
            </li>
          </ol>
        </div>
      </PopoverContent>
    </Popover>
  );
};

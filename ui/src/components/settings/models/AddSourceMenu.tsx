// 添加来源 (frame 07): connect a Claude / ChatGPT subscription (→ OAuth connect
// dialog) or add an API Key (→ form dialog). No type-chooser dialog on desktop;
// the menu IS the chooser.
//
// On phones it becomes a bottom sheet, and the flat three-entry list becomes the
// design's two-step chooser (design.pen M04 m04Sheet 0): 订阅账号 / API Key first,
// vendor second. Three vendor-specific rows read as three unrelated products on a
// small screen, where the actual decision the user is making is "subscription or
// key". Desktop keeps the flat list — frame 07 has the room for it, and the wide
// menu loses nothing by skipping a step.
import * as React from 'react';
import { ChevronLeft, ChevronRight, ExternalLink, KeyRound, Plus, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';

const MenuItem: React.FC<{
  Icon: React.ComponentType<{ size?: number; className?: string }>;
  accentTile: string;
  accentIcon: string;
  title: string;
  subtitle: string;
  badge?: string;
  Trailing: React.ComponentType<{ className?: string }>;
  onClick: () => void;
  /** Roomier type + wrapping subtitle for the touch sheet. */
  large?: boolean;
}> = ({ Icon, accentTile, accentIcon, title, subtitle, badge, Trailing, onClick, large }) => (
  <button
    type="button"
    onClick={onClick}
    className={cn(
      'flex w-full items-center gap-3 border-b border-border px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-surface-2',
      large && 'items-start gap-3.5 py-4',
    )}
  >
    <span className={cn('flex shrink-0 items-center justify-center rounded-[10px]', large ? 'size-11' : 'size-10', accentTile)}>
      <Icon size={large ? 22 : 20} className={accentIcon} />
    </span>
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="flex items-center gap-2">
        <span className={cn('font-semibold text-foreground', large ? 'text-[15px]' : 'text-[14px]')}>{title}</span>
        {badge && (
          <Badge variant="success" className="shrink-0 px-2 py-0.5 text-[10px]">
            {badge}
          </Badge>
        )}
      </span>
      <span className={cn('text-[12px] text-muted', large ? 'leading-relaxed' : 'truncate')}>{subtitle}</span>
    </span>
    <Trailing className={cn('size-4 shrink-0 text-muted', large && 'mt-1')} />
  </button>
);

export const AddSourceMenu: React.FC<{
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
}> = ({ onConnectClaude, onConnectChatGPT, onAddApiKey }) => {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const [open, setOpen] = React.useState(false);
  // Mobile only: which step of the chooser is showing.
  const [step, setStep] = React.useState<'type' | 'vendor'>('type');

  const close = () => setOpen(false);
  const pick = (fn: () => void) => () => {
    close();
    fn();
  };

  // Always reopen on the first step — landing back on the vendor list after a
  // completed connect would be a confusing place to start.
  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) setStep('type');
  };

  const claude = {
    Icon: Sparkles,
    accentTile: ACCENT_TILE.mint,
    accentIcon: ACCENT_ICON.mint,
    title: t('settings.models.addMenu.claude.title') as string,
    subtitle: t('settings.models.addMenu.claude.subtitle') as string,
    Trailing: ExternalLink,
    onClick: pick(onConnectClaude),
  };
  const chatgpt = {
    Icon: Sparkles,
    accentTile: ACCENT_TILE.gold,
    accentIcon: ACCENT_ICON.gold,
    title: t('settings.models.addMenu.chatgpt.title') as string,
    subtitle: t('settings.models.addMenu.chatgpt.subtitle') as string,
    Trailing: ExternalLink,
    onClick: pick(onConnectChatGPT),
  };
  const apiKey = {
    Icon: KeyRound,
    accentTile: ACCENT_TILE.violet,
    accentIcon: ACCENT_ICON.violet,
    title: t('settings.models.addMenu.apiKey.title') as string,
    subtitle: t('settings.models.addMenu.apiKey.subtitle') as string,
    Trailing: ChevronRight,
    onClick: pick(onAddApiKey),
  };

  const vendorStep = step === 'vendor';

  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={onOpenChange}
      align="end"
      sideOffset={8}
      // Never wider than the phone viewport (the fixed 360px overflowed a 360px
      // device once collision padding was applied).
      className="w-[min(360px,calc(100vw-1.5rem))] overflow-hidden p-0 sm:w-[360px]"
      sheetTitle={
        vendorStep
          ? (t('settings.models.addMenu.vendorTitle') as string)
          : (t('settings.models.addMenu.sheetTitle') as string)
      }
      sheetDescription={
        vendorStep
          ? (t('settings.models.addMenu.vendorSubtitle') as string)
          : (t('settings.models.addMenu.sheetSubtitle') as string)
      }
      sheetHeader={
        vendorStep ? (
          <div className="px-4 pb-2">
            <button
              type="button"
              onClick={() => setStep('type')}
              className="inline-flex min-h-10 items-center gap-1 text-[13px] font-medium text-mint transition-colors hover:text-mint/80"
            >
              <ChevronLeft className="size-4" />
              {t('common.back')}
            </button>
          </div>
        ) : undefined
      }
      trigger={
        <Button
          variant="outline"
          size="sm"
          // Full-width primary-looking button on phones (design.pen M01 m01AddBtn:
          // mint-soft fill, 1.5px mint stroke); the quieter inline button on sm+.
          className="h-11 w-full rounded-[11px] border-[1.5px] border-mint/70 bg-mint-soft text-mint hover:bg-mint-soft/80 sm:h-9 sm:w-auto sm:rounded-md sm:border sm:border-mint/40 sm:bg-mint-soft/50 sm:hover:bg-mint-soft"
        >
          <Plus className="size-4" />
          {t('settings.models.addSource')}
        </Button>
      }
    >
      {!isMobile ? (
        <>
          <MenuItem {...claude} />
          <MenuItem {...chatgpt} />
          <MenuItem {...apiKey} />
        </>
      ) : vendorStep ? (
        <>
          <MenuItem {...claude} large />
          <MenuItem {...chatgpt} large />
        </>
      ) : (
        <>
          <MenuItem
            Icon={Sparkles}
            accentTile={ACCENT_TILE.mint}
            accentIcon={ACCENT_ICON.mint}
            title={t('settings.models.addMenu.subscription.title') as string}
            subtitle={t('settings.models.addMenu.subscription.subtitle') as string}
            badge={t('settings.models.addMenu.subscription.recommended') as string}
            Trailing={ChevronRight}
            onClick={() => setStep('vendor')}
            large
          />
          <MenuItem {...apiKey} subtitle={t('settings.models.addMenu.apiKeySheetSubtitle') as string} large />
        </>
      )}
    </ResponsiveMenu>
  );
};

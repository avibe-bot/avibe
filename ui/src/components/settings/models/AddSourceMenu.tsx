// 添加来源 dropdown (frame 07): three entries — connect Claude / ChatGPT
// subscription (→ OAuth connect dialog) and add API Key (→ form dialog). No
// type-chooser dialog; the menu IS the chooser.
import * as React from 'react';
import { ChevronRight, ExternalLink, KeyRound, Plus, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';

type MenuItemProps = {
  Icon: React.ComponentType<{ size?: number; className?: string }>;
  accentTile: string;
  accentIcon: string;
  title: string;
  subtitle: string;
  Trailing: React.ComponentType<{ className?: string }>;
  onClick: () => void;
};

const MenuItem: React.FC<MenuItemProps> = ({ Icon, accentTile, accentIcon, title, subtitle, Trailing, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    className="flex w-full items-center gap-3 border-b border-border px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-surface-2"
  >
    <span className={cn('flex size-10 shrink-0 items-center justify-center rounded-[10px]', accentTile)}>
      <Icon size={20} className={accentIcon} />
    </span>
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="text-[14px] font-semibold text-foreground">{title}</span>
      <span className="truncate text-[12px] text-muted">{subtitle}</span>
    </span>
    <Trailing className="size-4 shrink-0 text-muted" />
  </button>
);

export const AddSourceMenu: React.FC<{
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
}> = ({ onConnectClaude, onConnectChatGPT, onAddApiKey }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);

  const pick = (fn: () => void) => () => {
    setOpen(false);
    fn();
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="border-mint/40 bg-mint-soft/50 text-mint hover:bg-mint-soft">
          <Plus className="size-4" />
          {t('settings.models.addSource')}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={8}
        className="w-[360px] overflow-hidden border-border bg-card p-0 text-foreground shadow-lg"
      >
        <MenuItem
          Icon={Sparkles}
          accentTile={ACCENT_TILE.mint}
          accentIcon={ACCENT_ICON.mint}
          title={t('settings.models.addMenu.claude.title')}
          subtitle={t('settings.models.addMenu.claude.subtitle')}
          Trailing={ExternalLink}
          onClick={pick(onConnectClaude)}
        />
        <MenuItem
          Icon={Sparkles}
          accentTile={ACCENT_TILE.gold}
          accentIcon={ACCENT_ICON.gold}
          title={t('settings.models.addMenu.chatgpt.title')}
          subtitle={t('settings.models.addMenu.chatgpt.subtitle')}
          Trailing={ExternalLink}
          onClick={pick(onConnectChatGPT)}
        />
        <MenuItem
          Icon={KeyRound}
          accentTile={ACCENT_TILE.violet}
          accentIcon={ACCENT_ICON.violet}
          title={t('settings.models.addMenu.apiKey.title')}
          subtitle={t('settings.models.addMenu.apiKey.subtitle')}
          Trailing={ChevronRight}
          onClick={pick(onAddApiKey)}
        />
      </PopoverContent>
    </Popover>
  );
};

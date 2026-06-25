import { useTranslation } from 'react-i18next';
import { TerminalSquare } from 'lucide-react';

// Placeholder for the Terminal app. The real terminal (xterm.js + WebSocket PTY +
// tmux-backed persistence) is Phase 2; the route + launcher entry exist now so the
// Apps shell is complete and the slot is reserved.
export const AppsTerminalPage: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-3xl flex-col items-center justify-center gap-3 text-center">
      <div className="grid size-14 place-items-center rounded-2xl border border-border-strong bg-foreground/[0.03] text-muted">
        <TerminalSquare className="size-7" />
      </div>
      <h1 className="text-[20px] font-semibold text-foreground">{t('apps.terminal.label')}</h1>
      <p className="max-w-md text-[13px] leading-relaxed text-muted">{t('apps.terminal.placeholder')}</p>
    </div>
  );
};

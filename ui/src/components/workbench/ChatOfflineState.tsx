import { CloudOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';

// What a chat shows when the server cannot be reached and it is reading what
// this device saved (docs/plans/local-sync-store.md, decision D4: one quiet
// connection notice and a marker where saved history ends; no per-conversation
// badge).

/** The chat's notice while it shows saved history because the server could not
 *  be reached. Information, not an error: the chat is readable. */
export function ChatOfflineNotice() {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      className="mx-auto mt-3 flex w-full max-w-[1080px] items-center gap-2 rounded-md border border-edge bg-surface-2 px-3 py-2 text-[12px] text-muted shadow-elevation-0"
    >
      <CloudOff aria-hidden className="size-3.5 shrink-0" />
      {t('chat.offline.notice')}
    </div>
  );
}

/** Where saved history ends and the server could not be reached for more: in
 *  the head slot of the transcript, in its fixed height, so nothing moves.
 *  Choosing it tries the server again. */
export function HistoryGapMarker({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onRetry}
      className="flex h-8 items-center justify-center gap-1.5 text-[12px] text-muted hover:text-foreground"
    >
      <CloudOff aria-hidden className="size-3.5" />
      {t('chat.offline.historyGap')}
    </button>
  );
}

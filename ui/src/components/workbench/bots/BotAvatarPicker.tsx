import { lazy, Suspense, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { Ellipsis, Loader2, RotateCcw, SmilePlus, Upload } from 'lucide-react';
import clsx from 'clsx';

import { BOT_AVATAR_EMOJIS, BOT_AVATAR_HUES } from '@/lib/bots';
import { useIsMobile } from '@/lib/useIsMobile';
import { Dialog, DialogContent, DialogTitle } from '../../ui/dialog';
import { SegmentedRadio } from '../../ui/segmented';

// Every emoji, with its data, loads only when "More" first opens.
const BotEmojiCatalog = lazy(() => import('./BotEmojiCatalog'));
const LEADING_EMOJIS: ReadonlySet<string> = new Set(BOT_AVATAR_EMOJIS);
/** Cells of about 44px, touch-sized, across the sheet's width (its padding is 20px a side). */
const sheetColumns = () => Math.max(8, Math.floor((window.innerWidth - 40) / 44));

/** What the user picked in the dialog, before anything is saved. */
export type BotAvatarChoice =
  | { kind: 'keep' }
  | { kind: 'default' }
  | { kind: 'emoji'; emoji: string; hue: number }
  | { kind: 'image'; file: File; previewUrl: string };

/** The server scales an upload to 256px; it refuses anything over 10 MB. */
export const BOT_AVATAR_MAX_BYTES = 10 * 1024 * 1024;
const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/webp'];

interface BotAvatarPickerProps {
  /** The emoji avatar the picker starts from, if the current choice is one. */
  current: { emoji: string; hue: number } | null;
  onPick: (choice: BotAvatarChoice) => void;
  /** Offered only when the Bot has something other than its default to undo. */
  canUseDefault: boolean;
  onError: (message: string) => void;
}

/** The avatar picker the add/edit dialog opens for a custom Bot (the main Bot keeps
 *  its mascot): an emoji on a hue, or an uploaded image that the server crops to a
 *  circle (D5). The emoji grid leads with one per common role; "More" opens every
 *  emoji, in this popover on desktop and as a sheet on a phone. AI-generated
 *  avatars are out of the first version. */
export const BotAvatarPicker: React.FC<BotAvatarPickerProps> = ({ current, onPick, canUseDefault, onError }) => {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const [tab, setTab] = useState<'emoji' | 'upload'>('emoji');
  const [emoji, setEmoji] = useState<string>(current?.emoji ?? BOT_AVATAR_EMOJIS[0]);
  const [hue, setHue] = useState<number>(current?.hue ?? BOT_AVATAR_HUES[0]);
  const [allOpen, setAllOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  // An emoji from the full set is the avatar's but has no cell here: "More" shows it.
  const pickedElsewhere = current !== null && !LEADING_EMOJIS.has(emoji) ? emoji : null;

  const pickEmoji = (nextEmoji: string, nextHue: number) => {
    setEmoji(nextEmoji);
    setHue(nextHue);
    onPick({ kind: 'emoji', emoji: nextEmoji, hue: nextHue });
  };

  const takeFile = (file: File | undefined) => {
    if (!file) return;
    if (!ACCEPTED_TYPES.includes(file.type)) {
      onError(t('bots.avatarPicker.unsupported'));
      return;
    }
    if (file.size > BOT_AVATAR_MAX_BYTES) {
      onError(t('bots.avatarPicker.tooLarge'));
      return;
    }
    onPick({ kind: 'image', file, previewUrl: URL.createObjectURL(file) });
  };

  const pickFromAll = (nextEmoji: string) => {
    pickEmoji(nextEmoji, hue);
    setAllOpen(false);
  };

  const loading = (
    <div className="flex flex-1 items-center justify-center gap-2 py-10 text-[12px] text-muted" role="status">
      <Loader2 className="size-3.5 animate-spin" />
      {t('bots.emojiPicker.loading')}
    </div>
  );

  if (allOpen && !isMobile) {
    return (
      <div className="flex h-[360px] flex-col">
        <Suspense fallback={loading}>
          <BotEmojiCatalog
            current={current !== null ? emoji : null}
            onPick={pickFromAll}
            onBack={() => setAllOpen(false)}
            columns={8}
            autoFocus
          />
        </Suspense>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <SegmentedRadio
        value={tab}
        onChange={setTab}
        ariaLabel={t('bots.dialog.avatar')}
        options={[
          { id: 'emoji', label: t('bots.avatarPicker.emoji') },
          { id: 'upload', label: t('bots.avatarPicker.upload') },
        ]}
      />
      {tab === 'emoji' ? (
        <>
          <div className="grid grid-cols-8 gap-1.5">
            {/* The radios lay out in the grid itself, so "More" takes the cell after the last one. */}
            <div className="contents" role="radiogroup" aria-label={t('bots.avatarPicker.emoji')}>
              {BOT_AVATAR_EMOJIS.map((value) => {
                const selected = current !== null && value === emoji;
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => pickEmoji(value, hue)}
                    className={clsx(
                      'grid aspect-square place-items-center rounded-lg text-[17px] transition',
                      selected ? 'bg-mint-soft ring-[1.5px] ring-inset ring-mint' : 'hover:bg-foreground/[0.05]',
                    )}
                  >
                    {value}
                  </button>
                );
              })}
            </div>
            <button
              type="button"
              onClick={() => setAllOpen(true)}
              aria-label={pickedElsewhere ? t('bots.avatarPicker.moreChosen', { emoji: pickedElsewhere }) : t('bots.avatarPicker.more')}
              title={pickedElsewhere ? t('bots.avatarPicker.moreChosen', { emoji: pickedElsewhere }) : t('bots.avatarPicker.more')}
              className={clsx(
                'relative grid aspect-square place-items-center rounded-lg transition',
                pickedElsewhere
                  ? 'bg-mint-soft text-[17px] ring-[1.5px] ring-inset ring-mint'
                  : 'text-muted hover:bg-foreground/[0.05] hover:text-foreground',
              )}
            >
              {pickedElsewhere ? (
                <>
                  {pickedElsewhere}
                  <span className="absolute -bottom-1 -right-1 grid size-3.5 place-items-center rounded-full border border-border-strong bg-surface-2 text-muted" aria-hidden="true">
                    <Ellipsis className="size-2.5" />
                  </span>
                </>
              ) : (
                <SmilePlus className="size-4" />
              )}
            </button>
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] font-semibold text-muted">{t('bots.avatarPicker.background')}</span>
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={t('bots.avatarPicker.background')}>
              {BOT_AVATAR_HUES.map((value) => {
                const selected = current !== null && value === hue;
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    aria-label={t('bots.avatarPicker.hue', { hue: value })}
                    onClick={() => pickEmoji(emoji, value)}
                    className={clsx(
                      'bot-avatar-emoji size-6 rounded-full transition',
                      selected && 'ring-2 ring-mint ring-offset-2 ring-offset-surface-2',
                    )}
                    style={{ ['--bot-hue' as string]: value } as CSSProperties}
                  />
                );
              })}
            </div>
          </div>
          <p className="text-[11px] leading-relaxed text-muted">{t('bots.avatarPicker.defaultHint')}</p>
        </>
      ) : (
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            takeFile(event.dataTransfer.files?.[0]);
          }}
          className={clsx(
            'flex flex-col items-center gap-2 rounded-xl border border-dashed px-4 py-6 text-center transition',
            dragging ? 'border-mint bg-mint-soft' : 'border-border-strong hover:bg-foreground/[0.03]',
          )}
        >
          <Upload className="size-5 text-muted" />
          <span className="text-[12px] font-semibold text-foreground">{t('bots.avatarPicker.dropHint')}</span>
          <span className="text-[11px] text-muted">{t('bots.avatarPicker.formats')}</span>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED_TYPES.join(',')}
            className="hidden"
            onChange={(event) => {
              takeFile(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
        </button>
      )}
      {canUseDefault && (
        <button
          type="button"
          onClick={() => onPick({ kind: 'default' })}
          className="flex items-center gap-1.5 self-start rounded-md px-1 text-[11px] font-medium text-muted transition hover:text-foreground"
        >
          <RotateCcw className="size-3" />
          {t('bots.avatarPicker.useDefault')}
        </button>
      )}
      {isMobile && (
        // A React child of the popover, so taps in the sheet do not dismiss it.
        <Dialog open={allOpen} onOpenChange={setAllOpen}>
          <DialogContent
            mobileSheetHeight="tall"
            closeLabel={t('common.close')}
            // Focus the sheet, not the search field: a phone keyboard would cover the list.
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              sheetRef.current?.focus();
            }}
            ref={sheetRef}
            className="flex flex-col gap-3 bg-surface-2"
          >
            <DialogTitle className="text-[15px] font-bold">{t('bots.avatarPicker.more')}</DialogTitle>
            <Suspense fallback={loading}>
              <BotEmojiCatalog current={current !== null ? emoji : null} onPick={pickFromAll} columns={sheetColumns()} />
            </Suspense>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
};

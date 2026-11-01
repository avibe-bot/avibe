import { useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { RotateCcw, Upload } from 'lucide-react';
import clsx from 'clsx';

import { BOT_AVATAR_EMOJIS, BOT_AVATAR_HUES } from '@/lib/bots';
import { SegmentedRadio } from '../../ui/segmented';

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

/** The avatar picker the add/edit dialog opens: an emoji on a hue, or an uploaded
 *  image that the server crops to a circle (D5). AI-generated avatars are out of
 *  the first version. */
export const BotAvatarPicker: React.FC<BotAvatarPickerProps> = ({ current, onPick, canUseDefault, onError }) => {
  const { t } = useTranslation();
  const [tab, setTab] = useState<'emoji' | 'upload'>('emoji');
  const [emoji, setEmoji] = useState<string>(current?.emoji ?? BOT_AVATAR_EMOJIS[0]);
  const [hue, setHue] = useState<number>(current?.hue ?? BOT_AVATAR_HUES[0]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

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
          <div className="grid grid-cols-8 gap-1.5" role="radiogroup" aria-label={t('bots.avatarPicker.emoji')}>
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
    </div>
  );
};

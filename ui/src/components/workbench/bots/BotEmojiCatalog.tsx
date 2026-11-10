// Every emoji a Bot's avatar can be (docs/plans/bots.md D5), behind the avatar
// picker's "More". Loaded on demand (`React.lazy` in BotAvatarPicker), together
// with its data, so neither the dialog nor the main bundle carries either.
//
// Frimousse (unstyled, about 10 kB gzipped) draws the virtualized grid and owns
// search and keyboard movement; the data is the build-time catalog
// (`virtual:emoji-catalog`), never a CDN. Emoji newer than this system draws are
// left out, as are flags where the system spells them as letters.
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ComponentProps } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, Loader2, Search } from 'lucide-react';
import clsx from 'clsx';
import {
  EmojiPicker,
  useSkinTone,
  type EmojiData,
  type EmojiPickerListCategoryHeaderProps,
  type EmojiPickerListEmojiProps,
  type SkinTone,
} from 'frimousse';
import catalog, { type EmojiCatalogEntry } from 'virtual:emoji-catalog';

const RECENTS_STORAGE_KEY = 'avibe.bots.emoji-recents.v1';
const SKIN_TONE_STORAGE_KEY = 'avibe.bots.emoji-skin-tone.v1';
const RECENTS_KEPT = 16;

const SKIN_TONES = ['light', 'medium-light', 'medium', 'medium-dark', 'dark'] as const;
/** Emojibase groups in display order; 2 (skin-tone and hair components) is not in the catalog. */
const GROUPS = [
  [0, 'smileys'],
  [1, 'people'],
  [3, 'animals'],
  [4, 'food'],
  [5, 'travel'],
  [6, 'activities'],
  [7, 'objects'],
  [8, 'symbols'],
  [9, 'flags'],
] as const;

// --- what this system draws -------------------------------------------------

const EMOJI_FONT = "'Apple Color Emoji', 'Noto Color Emoji', 'Twemoji Mozilla', 'Android Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol', EmojiSymbols, sans-serif";

/** Whether `emoji` is drawn as one color glyph: a sequence the font lacks comes
 *  out as several glyphs (wider), and a missing glyph as a monochrome fallback
 *  (it takes the fill color, which a color emoji ignores). */
function drawsAsOneColorGlyph(context: CanvasRenderingContext2D, emoji: string): boolean {
  const size = 2;
  context.canvas.width = size;
  context.canvas.height = size;
  context.font = `${size}px ${EMOJI_FONT}`;
  context.textBaseline = 'middle';
  if (context.measureText(emoji).width >= size * 2) return false;
  const pixels = (fill: string) => {
    context.clearRect(0, 0, size, size);
    context.fillStyle = fill;
    context.fillText(emoji, 0, 0);
    return context.getImageData(0, 0, size, size).data;
  };
  const blue = pixels('#00f');
  const red = pixels('#f00');
  for (let index = 0; index < blue.length; index += 4) {
    if (blue[index] !== red[index] || blue[index + 1] !== red[index + 1] || blue[index + 2] !== red[index + 2]) return false;
  }
  return true;
}

type SystemSupport = { version: number; flags: boolean } | null;
let systemSupport: SystemSupport | undefined;

/** The newest Emoji version this system draws, tried with the first emoji of each
 *  version, newest first; `null` when it cannot tell, and then nothing is left out. */
function emojiSupport(entries: readonly EmojiCatalogEntry[]): SystemSupport {
  if (systemSupport !== undefined) return systemSupport;
  let context: CanvasRenderingContext2D | null = null;
  try {
    context = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  } catch {
    context = null;
  }
  if (!context) return (systemSupport = null);
  const firstOfVersion = new Map<number, string>();
  for (const [emoji, , version] of entries) if (!firstOfVersion.has(version)) firstOfVersion.set(version, emoji);
  const newestFirst = [...firstOfVersion.keys()].sort((a, b) => b - a);
  const version = newestFirst.find((candidate) => drawsAsOneColorGlyph(context, firstOfVersion.get(candidate)!));
  systemSupport = version === undefined ? null : { version, flags: drawsAsOneColorGlyph(context, '🇪🇺') };
  return systemSupport;
}

// --- remembered picks -------------------------------------------------------

function readRecents(): string[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENTS_STORAGE_KEY) ?? '[]') as unknown;
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === 'string').slice(0, RECENTS_KEPT) : [];
  } catch {
    return [];
  }
}

function rememberRecent(emoji: string): void {
  try {
    const next = [emoji, ...readRecents().filter((value) => value !== emoji)].slice(0, RECENTS_KEPT);
    window.localStorage.setItem(RECENTS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage full or unavailable: the pick itself still lands.
  }
}

function readSkinTone(): SkinTone {
  try {
    const stored = window.localStorage.getItem(SKIN_TONE_STORAGE_KEY);
    return (SKIN_TONES as readonly string[]).includes(stored ?? '') ? (stored as SkinTone) : 'none';
  } catch {
    return 'none';
  }
}

function rememberSkinTone(tone: SkinTone): void {
  try {
    window.localStorage.setItem(SKIN_TONE_STORAGE_KEY, tone);
  } catch {
    // As above: only the next visit forgets it.
  }
}

// --- the list ---------------------------------------------------------------

const CurrentEmoji = createContext<string | null>(null);

const capitalized = (text: string) => text.charAt(0).toLocaleUpperCase() + text.slice(1);

function CategoryHeader({ category, className, ...props }: EmojiPickerListCategoryHeaderProps) {
  return (
    <div {...props} className={clsx('bg-surface-2 px-1 pb-1 pt-2 text-[11px] font-semibold text-muted', className)}>
      {category.label}
    </div>
  );
}

function Row({ className, ...props }: ComponentProps<'div'>) {
  return <div {...props} className={clsx('scroll-my-1', className)} />;
}

const cellClass = 'grid aspect-square shrink-0 basis-[calc(100%/var(--frimousse-list-columns))] place-items-center rounded-lg text-[20px] leading-none transition-colors';

function Emoji({ emoji, className, ...props }: EmojiPickerListEmojiProps) {
  const selected = useContext(CurrentEmoji) === emoji.emoji;
  return (
    <button
      {...props}
      type="button"
      // A grid cell, so "the avatar's emoji" rather than a toggle state.
      aria-current={selected || undefined}
      className={clsx(
        cellClass,
        selected ? 'bg-mint-soft ring-[1.5px] ring-inset ring-mint' : emoji.isActive && 'bg-foreground/[0.06]',
        className,
      )}
    >
      {emoji.emoji}
    </button>
  );
}

const LIST_COMPONENTS = { CategoryHeader, Row, Emoji };

const SKIN_TONE_SAMPLE = '✋';

/** Cycles the skin tone the list shows, remembered for the next visit. */
function SkinToneButton({ toneLabels }: { toneLabels: Record<SkinTone, string> }) {
  const { t } = useTranslation();
  const [tone, setTone, variations] = useSkinTone(SKIN_TONE_SAMPLE);
  const index = Math.max(0, variations.findIndex((variation) => variation.skinTone === tone));
  const shown = variations[index];
  const label = t('bots.emojiPicker.skinTone', { tone: toneLabels[tone] });
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={() => {
        const next = variations[(index + 1) % variations.length]?.skinTone ?? 'none';
        setTone(next);
        rememberSkinTone(next);
      }}
      className="grid size-8 shrink-0 place-items-center rounded-lg text-[17px] leading-none transition hover:bg-wash"
    >
      {/* Empty only until the data loads. */}
      {shown?.emoji}
    </button>
  );
}

export interface BotEmojiCatalogProps {
  /** The emoji the avatar has now, marked where it appears. */
  current: string | null;
  onPick: (emoji: string) => void;
  /** Back to the leading set, where it shares the picker's popover (desktop). */
  onBack?: () => void;
  columns: number;
  autoFocus?: boolean;
}

export default function BotEmojiCatalog({ current, onPick, onBack, columns, autoFocus }: BotEmojiCatalogProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  const [search, setSearch] = useState('');
  const [recents] = useState(readRecents);
  const [initialSkinTone] = useState(readSkinTone);

  const toneLabels = useMemo<Record<SkinTone, string>>(() => ({
    none: t('bots.emojiPicker.tones.none'),
    light: t('bots.emojiPicker.tones.light'),
    'medium-light': t('bots.emojiPicker.tones.mediumLight'),
    medium: t('bots.emojiPicker.tones.medium'),
    'medium-dark': t('bots.emojiPicker.tones.mediumDark'),
    dark: t('bots.emojiPicker.tones.dark'),
  }), [t]);

  // A name and keywords in the reader's language lead; the other language's come
  // after them, so a search in either finds the emoji.
  const resolveEmojiData = useCallback((): EmojiData => {
    const support = emojiSupport(catalog);
    const emojis: EmojiData['emojis'] = [];
    for (const [emoji, group, version, flag, en, zh, skins] of catalog) {
      if (support && (version > support.version || (flag === 1 && !support.flags))) continue;
      const [own, other] = locale === 'zh' && zh.length > 0 ? [zh, en] : [en, zh];
      emojis.push({
        emoji,
        category: group,
        version,
        label: capitalized(own[0] ?? emoji),
        tags: [...own.slice(1), ...other],
        skins: skins
          ? Object.fromEntries(SKIN_TONES.map((tone, index) => [tone, skins[index]])) as Record<(typeof SKIN_TONES)[number], string>
          : undefined,
      });
    }
    return {
      locale,
      emojis,
      categories: GROUPS.map(([index, key]) => ({ index, label: t(`bots.emojiPicker.groups.${key}`) })),
      skinTones: {
        light: toneLabels.light,
        'medium-light': toneLabels['medium-light'],
        medium: toneLabels.medium,
        'medium-dark': toneLabels['medium-dark'],
        dark: toneLabels.dark,
      },
    };
  }, [locale, t, toneLabels]);

  const pick = (emoji: string) => {
    rememberRecent(emoji);
    onPick(emoji);
  };

  return (
    <CurrentEmoji.Provider value={current}>
      <EmojiPicker.Root
        locale={locale}
        resolveEmojiData={resolveEmojiData}
        columns={columns}
        skinTone={initialSkinTone}
        onEmojiSelect={({ emoji }) => pick(emoji)}
        className="flex min-h-0 flex-1 flex-col gap-2"
      >
        <div className="flex items-center gap-1">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              aria-label={t('bots.emojiPicker.back')}
              title={t('bots.emojiPicker.back')}
              className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-wash hover:text-foreground"
            >
              <ChevronLeft className="size-4" />
            </button>
          )}
          <label className="relative flex min-w-0 flex-1 items-center">
            <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted" aria-hidden="true" />
            <EmojiPicker.Search
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              autoFocus={autoFocus}
              placeholder={t('bots.emojiPicker.search')}
              aria-label={t('bots.emojiPicker.search')}
              className="h-8 w-full min-w-0 rounded-lg border border-field-edge bg-field pl-8 pr-2.5 text-[13px] text-foreground outline-none transition placeholder:text-muted focus-visible:border-mint/60 focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-search-cancel-button]:hidden"
            />
          </label>
          <SkinToneButton toneLabels={toneLabels} />
        </div>
        {search.trim() === '' && recents.length > 0 && (
          <div className="flex flex-col">
            <span className="px-1 pb-1 text-[11px] font-semibold text-muted">{t('bots.emojiPicker.recent')}</span>
            <div className="flex" role="group" aria-label={t('bots.emojiPicker.recent')} style={{ ['--frimousse-list-columns' as string]: columns }}>
              {recents.slice(0, columns).map((emoji) => (
                <button
                  key={emoji}
                  type="button"
                  aria-pressed={emoji === current}
                  onClick={() => pick(emoji)}
                  className={clsx(
                    cellClass,
                    emoji === current ? 'bg-mint-soft ring-[1.5px] ring-inset ring-mint' : 'hover:bg-wash',
                  )}
                >
                  {emoji}
                </button>
              ))}
            </div>
          </div>
        )}
        <EmojiPicker.Viewport className="relative min-h-0 flex-1 outline-none">
          <EmojiPicker.Loading className="absolute inset-0 flex items-center justify-center gap-2 text-[12px] text-muted">
            <Loader2 className="size-3.5 animate-spin" />
            {t('bots.emojiPicker.loading')}
          </EmojiPicker.Loading>
          <EmojiPicker.Empty className="absolute inset-0 flex items-center justify-center px-4 text-center text-[12px] text-muted">
            {t('bots.emojiPicker.empty')}
          </EmojiPicker.Empty>
          <EmojiPicker.List components={LIST_COMPONENTS} className="select-none pb-1" />
        </EmojiPicker.Viewport>
      </EmojiPicker.Root>
    </CurrentEmoji.Provider>
  );
}

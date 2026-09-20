import { useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, CircleCheck, Columns3, Monitor, Moon, PanelLeft, Sun } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel } from './SettingsPrimitives';
import { Select } from '../ui/select';
import { useTheme } from '../../context/ThemeContext';
import type { ThemeMode } from '../../context/ThemeContext';
import { useLanguageSelection } from '../../lib/useLanguageSelection';
import {
  useSettingsMenuPlacement,
  writeSettingsMenuPlacement,
} from '../../lib/settingsMenuPlacement';
import type { SettingsMenuPlacement } from '../../lib/settingsMenuPlacement';
import type { TranslationKey } from '../../i18n/types';

// The 88px miniature is a drawing OF a theme, not a preview rendered IN one:
// the Light card has to look light while the app is dark. Source Q8zxF1 builds
// these from literal hex rather than tokens, so these are those exact values.
const PALETTE = {
  light: { shell: '#f4f6fb', chrome: '#ffffff', bar: '#5c6079' },
  dark: { shell: '#080812', chrome: '#11111c', bar: '#9ba3b8' },
} as const;

const MiniatureBody: React.FC<{ tone: keyof typeof PALETTE }> = ({ tone }) => {
  const c = PALETTE[tone];
  return (
    <div className="absolute inset-0 flex" style={{ background: c.shell }}>
      <div className="flex w-[38px] shrink-0 flex-col gap-[5px] p-2" style={{ background: c.chrome }}>
        <div className="flex items-center gap-[3px]">
          {[0, 1, 2].map((i) => (
            <span key={i} className="size-[4px] rounded-full" style={{ background: c.bar, opacity: 0.45 }} />
          ))}
        </div>
        <div className="h-[4px] w-full rounded-sm" style={{ background: c.bar, opacity: 0.45 }} />
        <div className="h-[4px] w-[78%] rounded-sm" style={{ background: c.bar, opacity: 0.45 }} />
        <div className="h-[4px] w-[62%] rounded-sm" style={{ background: c.bar, opacity: 0.45 }} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-[6px] p-2.5">
        <div className="h-[6px] w-[55%] rounded-sm" style={{ background: c.bar, opacity: 0.65 }} />
        <div className="h-[4px] w-[85%] rounded-sm" style={{ background: c.bar, opacity: 0.35 }} />
        <div className="h-[4px] w-[70%] rounded-sm" style={{ background: c.bar, opacity: 0.35 }} />
        <div className="mt-auto h-[16px] w-full rounded" style={{ background: c.chrome }} />
      </div>
    </div>
  );
};

/**
 * `system` is drawn as one app split diagonally rather than as whichever theme
 * the OS currently reports. The source renders it identically to `dark` in both
 * boards, so "follow the system" and "dark" are told apart only by their icon;
 * binding it to the resolved theme instead would make it identical to whichever
 * of the other two is live. Only the representation changes — both halves use
 * the source's own Light and Dark swatches.
 */
const ThemeMiniature: React.FC<{ mode: ThemeMode }> = ({ mode }) => (
  <div aria-hidden="true" className="relative h-[88px] w-full overflow-hidden rounded-sm">
    <MiniatureBody tone={mode === 'dark' ? 'dark' : 'light'} />
    {mode === 'system' && (
      <div className="absolute inset-0" style={{ clipPath: 'polygon(100% 0, 100% 100%, 0 100%)' }}>
        <MiniatureBody tone="dark" />
      </div>
    )}
  </div>
);

/**
 * Where the Settings menu sits, drawn from live tokens rather than the literal
 * palette above: this miniature is about layout, so unlike the theme cards it
 * SHOULD repaint with the active theme. Mint marks the Settings menu itself, so
 * the eye can follow that one column between the two cards — the neutral column
 * that appears before it in `inline` is the app sidebar the choice preserves.
 */
const PlacementMiniature: React.FC<{ placement: SettingsMenuPlacement }> = ({ placement }) => (
  <div
    aria-hidden="true"
    className="flex h-[88px] w-full overflow-hidden rounded-sm border border-border bg-background"
  >
    {placement === 'inline' && (
      <div className="flex w-[26px] shrink-0 flex-col gap-[5px] border-r border-border bg-[var(--sidebar-background)] p-1.5">
        <div className="h-[4px] w-full rounded-sm bg-muted/40" />
        <div className="h-[4px] w-[70%] rounded-sm bg-muted/40" />
        <div className="h-[4px] w-[85%] rounded-sm bg-muted/40" />
      </div>
    )}
    <div
      className={clsx(
        'flex shrink-0 flex-col gap-[5px] border-r border-mint/30 bg-mint-soft p-1.5',
        placement === 'inline' ? 'w-[30px]' : 'w-[38px]',
      )}
    >
      <div className="h-[4px] w-[80%] rounded-sm bg-mint-ink/50" />
      <div className="h-[4px] w-full rounded-sm bg-mint-ink/25" />
      <div className="h-[4px] w-[65%] rounded-sm bg-mint-ink/25" />
      <div className="h-[4px] w-[75%] rounded-sm bg-mint-ink/25" />
    </div>
    <div className="flex min-w-0 flex-1 flex-col gap-[6px] p-2.5">
      <div className="h-[6px] w-[55%] rounded-sm bg-muted/60" />
      <div className="h-[13px] w-full rounded border border-border" />
      <div className="h-[13px] w-full rounded border border-border" />
    </div>
  </div>
);

type ChoiceCard<T extends string> = {
  value: T;
  icon: LucideIcon;
  labelKey: TranslationKey;
  miniature: React.ReactNode;
};

type ChoiceCardsProps<T extends string> = {
  label: string;
  choices: readonly ChoiceCard<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
};

/**
 * The drawn-choice control the page uses twice. A radiogroup is one tab stop
 * that arrows through its options; separately tabbable buttons would only look
 * like radios.
 */
function ChoiceCards<T extends string>({
  label,
  choices,
  value,
  onChange,
  className,
}: ChoiceCardsProps<T>) {
  const { t } = useTranslation();
  const cardRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const moveTo = (index: number) => {
    const next = (index + choices.length) % choices.length;
    onChange(choices[next].value);
    cardRefs.current[next]?.focus();
  };

  const onCardKeyDown = (event: React.KeyboardEvent, index: number) => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') moveTo(index + 1);
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') moveTo(index - 1);
    else if (event.key === 'Home') moveTo(0);
    else if (event.key === 'End') moveTo(choices.length - 1);
    else return;
    event.preventDefault();
  };

  return (
    <div role="radiogroup" aria-label={label} className={clsx('grid gap-3.5', className)}>
      {choices.map(({ value: choice, icon: Icon, labelKey, miniature }, index) => {
        const selected = value === choice;
        return (
          <button
            key={choice}
            ref={(node) => { cardRefs.current[index] = node; }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(choice)}
            onKeyDown={(event) => onCardKeyDown(event, index)}
            className={clsx(
              'flex flex-col gap-2.5 rounded-xl text-left transition-colors',
              // The selected border is 2px in the source; padding absorbs the
              // extra pixel so picking a card never nudges the row.
              selected
                ? 'border-2 border-mint bg-mint-soft p-[9px]'
                : 'border border-border-strong bg-surface p-2.5 hover:bg-foreground/[0.03]',
            )}
          >
            {miniature}
            <span className="flex items-center gap-2">
              <Icon className={clsx('size-3.5 shrink-0', selected ? 'text-mint-ink' : 'text-muted')} />
              <span className={clsx('flex-1 text-[12px] font-medium', selected ? 'text-foreground' : 'text-muted')}>
                {t(labelKey)}
              </span>
              {selected && <CircleCheck className="size-3.5 shrink-0 text-mint-ink" />}
            </span>
          </button>
        );
      })}
    </div>
  );
}

const THEME_CHOICES: readonly ChoiceCard<ThemeMode>[] = [
  {
    value: 'system',
    icon: Monitor,
    labelKey: 'settings.general.appearanceSystem',
    miniature: <ThemeMiniature mode="system" />,
  },
  {
    value: 'light',
    icon: Sun,
    labelKey: 'settings.general.appearanceLight',
    miniature: <ThemeMiniature mode="light" />,
  },
  {
    value: 'dark',
    icon: Moon,
    labelKey: 'settings.general.appearanceDark',
    miniature: <ThemeMiniature mode="dark" />,
  },
];

const PLACEMENT_CHOICES: readonly ChoiceCard<SettingsMenuPlacement>[] = [
  {
    value: 'standalone',
    icon: PanelLeft,
    labelKey: 'settings.general.menuPlacementStandalone',
    miniature: <PlacementMiniature placement="standalone" />,
  },
  {
    value: 'inline',
    icon: Columns3,
    labelKey: 'settings.general.menuPlacementInline',
    miniature: <PlacementMiniature placement="inline" />,
  },
];

// design r6G6P — ordinary Settings: interface language and appearance, both
// saved the moment they are picked. The standalone frame owns an 880px content
// column inside a 944px outer frame at the desktop reference width.
export const SettingsGeneralPage: React.FC = () => {
  const { t } = useTranslation();
  const { languages, current, select } = useLanguageSelection();
  const { mode, setMode } = useTheme();
  const placement = useSettingsMenuPlacement();

  return (
    <SettingsPageShell
      title={t('settings.general.title')}
      subtitle={t('settings.general.subtitle')}
      activeTab="general"
      titleScale="landing"
    >
      <div className="flex flex-col gap-6">
        {/* Language le5QU — label and selector are one row, no divider. */}
        <SettingsPanel
          variant="preference"
          title={t('settings.general.languageTitle')}
          description={t('settings.general.languageDescription')}
          actions={
            <Select
              aria-label={t('settings.general.languageTitle')}
              value={current.code}
              onChange={(event) => void select(event.target.value)}
              wrapperClassName="w-[190px] max-w-full"
              className="h-10 rounded-[9px] border-border-strong bg-surface px-3.5 pr-9 text-[13px]"
            >
              {languages.map((lang) => (
                <option key={lang.code} value={lang.code}>{lang.label}</option>
              ))}
            </Select>
          }
        />

        {/* Appearance Q8zxF1 — three drawn choices, selection is real state. */}
        <SettingsPanel
          variant="preference"
          title={t('settings.general.appearanceTitle')}
          description={t('settings.general.appearanceDescription')}
        >
          <ChoiceCards
            label={t('settings.general.appearanceTitle')}
            choices={THEME_CHOICES}
            value={mode}
            onChange={setMode}
            className="grid-cols-1 sm:grid-cols-3"
          />
        </SettingsPanel>

        {/* Settings menu placement. Hidden below md: the Settings surface is
            full-screen on a phone whatever this says, and a control that
            changes nothing on the viewport you are holding is not a choice. */}
        <SettingsPanel
          variant="preference"
          className="hidden md:block"
          title={t('settings.general.menuPlacementTitle')}
          description={t('settings.general.menuPlacementDescription')}
        >
          <ChoiceCards
            label={t('settings.general.menuPlacementTitle')}
            choices={PLACEMENT_CHOICES}
            value={placement}
            onChange={writeSettingsMenuPlacement}
            className="grid-cols-1 sm:grid-cols-2"
          />
        </SettingsPanel>

        {/* i3LvLl — the autosave contract, stated once for the whole page. */}
        <div className="flex items-center gap-2 text-[12px] text-muted">
          <Check className="size-[15px] shrink-0 text-mint-ink" />
          <span>{t('settings.general.autosaved')}</span>
        </div>
      </div>
    </SettingsPageShell>
  );
};

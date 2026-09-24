import React, { useEffect, useRef, useState } from 'react';
import { Check, Languages } from 'lucide-react';
import clsx from 'clsx';
import { useLanguageSelection } from '../lib/useLanguageSelection';

interface LanguageSwitcherProps {
  openUpward?: boolean;
  /** `label` is the compact square used by the settings shells; `icon-round` is
      the circular Languages button the onboarding header draws. Same menu, same
      state handling, same selection flow — only the trigger and the wrapper
      classes differ. */
  variant?: 'label' | 'icon-round';
}

export const LanguageSwitcher: React.FC<LanguageSwitcherProps> = ({ openUpward = false, variant = 'label' }) => {
  const { languages, current: currentLang, select } = useLanguageSelection();
  const [isOpen, setIsOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsOpen(false);
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('keydown', handleEscape);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isOpen]);

  const shortLabel = (code: string) => {
    if (code === 'zh') return '中';
    return code.slice(0, 2).toUpperCase();
  };

  const handleSelect = async (code: string) => {
    setIsOpen(false);
    await select(code);
  };

  const round = variant === 'icon-round';
  return (
    <div className={round ? 'onboarding-language' : 'relative'} ref={wrapRef}>
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        aria-label={currentLang.label}
        title={currentLang.label}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        className={round
          ? 'onboarding-language-button'
          : 'inline-flex h-8 w-8 items-center justify-center rounded-md border border-border-strong bg-surface-2/40 text-[11px] font-semibold text-muted transition hover:bg-surface-2 hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring'}
      >
        {round ? <Languages size={22} /> : shortLabel(currentLang.code)}
      </button>

      {isOpen && (
        <div
          role="listbox"
          className={round
            ? 'onboarding-language-menu'
            : clsx(
                'absolute z-50 min-w-[10rem] rounded-lg border border-border bg-popover py-1 text-popover-foreground shadow-xl',
                openUpward ? 'bottom-full left-0 mb-2' : 'top-full right-0 mt-2'
              )}
        >
          {languages.map((lang) => {
            const active = lang.code === currentLang.code;
            return (
              <button
                key={lang.code}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => handleSelect(lang.code)}
                className={clsx(
                  // `--surface-2` is white on a light theme, which is also the menu's own
                  // surface: a row highlighted with it reads as no highlight at all.
                  'flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition-colors hover:bg-foreground/[0.06] hover:text-foreground',
                  active ? 'bg-mint-soft text-foreground' : 'text-muted'
                )}
              >
                <span>{lang.label}</span>
                {active && <Check size={14} className="text-mint-ink" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

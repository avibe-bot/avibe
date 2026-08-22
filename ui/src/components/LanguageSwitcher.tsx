import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check } from 'lucide-react';
import clsx from 'clsx';
import { useApi } from '../context/ApiContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { setConfigField } from '../lib/configMutations';

export const LanguageSwitcher: React.FC<{ openUpward?: boolean }> = ({ openUpward = false }) => {
  const { i18n, t } = useTranslation();
  const { getConfig, mutateConfig } = useApi();
  const { capabilities } = useInstanceAuthorization();
  const [isOpen, setIsOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!capabilities.can_manage_instance) return;
    const loadConfig = async () => {
      try {
        const cfg = await getConfig();
        if (cfg.language && cfg.language !== i18n.language) {
          i18n.changeLanguage(cfg.language);
        }
      } catch {
        // Ignore errors on config load
      }
    };
    loadConfig();
  }, [capabilities.can_manage_instance, getConfig, i18n]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  const languageCodes = Object.keys(i18n.options.resources ?? {});
  const availableLanguages = languageCodes.length ? languageCodes : ['en'];
  const languages = availableLanguages.map((code) => ({
    code,
    label: t(`language.${code}`, { defaultValue: code }),
  }));
  const currentLang = languages.find((l) => l.code === i18n.language) || languages[0];

  const shortLabel = (code: string) => {
    if (code === 'zh') return '中';
    return code.slice(0, 2).toUpperCase();
  };

  const handleSelect = async (code: string) => {
    setIsOpen(false);
    if (code === i18n.language) return;
    i18n.changeLanguage(code);
    if (!capabilities.can_manage_instance) return;
    try {
      await mutateConfig([setConfigField(['language'], code)]);
    } catch {
      // Ignore save errors - language change already applied locally
    }
  };

  return (
    <div className="relative" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        aria-label={currentLang.label}
        title={currentLang.label}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border-strong bg-surface-2/40 text-[11px] font-semibold text-muted transition hover:bg-surface-2 hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      >
        {shortLabel(currentLang.code)}
      </button>

      {isOpen && (
        <div
          role="listbox"
          className={clsx(
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
                  'flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition-colors',
                  active
                    ? 'text-foreground'
                    : 'text-muted hover:bg-surface-2 hover:text-foreground'
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

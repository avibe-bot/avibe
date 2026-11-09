import React from 'react';
import { Monitor, Moon, Sun } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useTheme } from '@/context/ThemeContext';

export const ThemeToggle: React.FC = () => {
  const { t } = useTranslation();
  const { mode, cycleMode } = useTheme();

  const Icon = mode === 'system' ? Monitor : mode === 'light' ? Sun : Moon;
  const label =
    mode === 'system'
      ? t('common.themeSystem')
      : mode === 'light'
        ? t('common.themeLight')
        : t('common.themeDark');

  return (
    <button
      type="button"
      onClick={cycleMode}
      aria-label={`${label}. ${t('common.themeToggleHint')}`}
      title={label}
      className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-edge text-muted transition hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring bg-field hover:bg-field-hover active:bg-field-hover"
    >
      <Icon className="size-3.5" aria-hidden />
    </button>
  );
};

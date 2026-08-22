import React, { useEffect, useMemo, useState } from 'react';
import { LogOut, Monitor, Moon, Sun } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useTheme } from '@/context/ThemeContext';
import type { ThemeMode } from '@/context/ThemeContext';
import { setConfigField } from '@/lib/configMutations';
import { useAuthAccount } from '@/lib/useAuthAccount';
import { Button } from '@/components/ui/button';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel, SettingsRow } from './SettingsPrimitives';

const THEME_OPTIONS: Array<{ mode: ThemeMode; icon: React.ComponentType<{ className?: string }>; labelKey: string }> = [
  { mode: 'system', icon: Monitor, labelKey: 'common.themeSystem' },
  { mode: 'light', icon: Sun, labelKey: 'common.themeLight' },
  { mode: 'dark', icon: Moon, labelKey: 'common.themeDark' },
];

export const SettingsAppearancePage: React.FC = () => {
  const { t, i18n } = useTranslation();
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { mode, setMode } = useTheme();
  const [savingLanguage, setSavingLanguage] = useState(false);
  const languageCodes = useMemo(() => {
    const codes = Object.keys(i18n.options.resources ?? {});
    return codes.length > 0 ? codes : ['en'];
  }, [i18n.options.resources]);

  useEffect(() => {
    if (!capabilities.can_manage_instance) return;
    void api.getConfig()
      .then((config) => {
        if (config.language && config.language !== i18n.language) {
          void i18n.changeLanguage(config.language);
        }
      })
      .catch(() => {});
  }, [api, capabilities.can_manage_instance, i18n]);

  const selectLanguage = async (language: string) => {
    if (language === i18n.language) return;
    setSavingLanguage(true);
    await i18n.changeLanguage(language);
    if (!capabilities.can_manage_instance) {
      setSavingLanguage(false);
      return;
    }
    try {
      await api.mutateConfig([setConfigField(['language'], language)]);
    } catch {
      // The browser preference is already applied; the instance save can retry later.
    } finally {
      setSavingLanguage(false);
    }
  };

  return (
    <SettingsPageShell
      activeTab="appearance"
      title={t('settings.appearanceTitle')}
      subtitle={t('settings.appearanceSubtitle')}
    >
      <SettingsPanel title={t('settings.themeTitle')}>
        <SettingsRow
          title={t('settings.themeMode')}
          description={t('settings.themeModeHint')}
          control={
            <div className="flex flex-wrap gap-2">
              {THEME_OPTIONS.map((option) => {
                const Icon = option.icon;
                return (
                  <Button
                    key={option.mode}
                    type="button"
                    variant={mode === option.mode ? 'brand' : 'secondary'}
                    size="sm"
                    onClick={() => setMode(option.mode)}
                  >
                    <Icon className="size-3.5" />
                    {t(option.labelKey)}
                  </Button>
                );
              })}
            </div>
          }
        />
      </SettingsPanel>

      <SettingsPanel title={t('settings.languageTitle')}>
        <SettingsRow
          title={t('settings.interfaceLanguage')}
          description={t('settings.interfaceLanguageHint')}
          control={
            <div className="flex flex-wrap gap-2">
              {languageCodes.map((language) => (
                <Button
                  key={language}
                  type="button"
                  variant={i18n.language === language ? 'brand' : 'secondary'}
                  size="sm"
                  disabled={savingLanguage}
                  onClick={() => void selectLanguage(language)}
                >
                  {t(`language.${language}`, { defaultValue: language })}
                </Button>
              ))}
            </div>
          }
        />
      </SettingsPanel>
    </SettingsPageShell>
  );
};

export const SettingsAccountPage: React.FC = () => {
  const { t } = useTranslation();
  const { email, signingOut, signOut } = useAuthAccount();

  return (
    <SettingsPageShell
      activeTab="account"
      title={t('settings.accountTitle')}
      subtitle={t('settings.accountSubtitle')}
    >
      <SettingsPanel title={t('settings.identityTitle')}>
        <SettingsRow
          title={t('settings.signedInIdentity')}
          description={email ?? t('settings.localSession')}
          control={email ? (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={signingOut}
              onClick={() => void signOut()}
              className={clsx(signingOut && 'opacity-70')}
            >
              <LogOut className="size-3.5" />
              {signingOut ? t('appShell.signingOut') : t('appShell.signOut')}
            </Button>
          ) : null}
        />
      </SettingsPanel>
    </SettingsPageShell>
  );
};

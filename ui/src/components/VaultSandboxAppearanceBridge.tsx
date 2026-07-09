import { useLayoutEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../context/ThemeContext';
import { getActiveVaultSandboxClient } from '../lib/vaultSandboxClient';
import {
  normalizeVaultSandboxLocale,
  setVaultSandboxAppearance,
  type VaultSandboxAppearance,
} from '../lib/vaultSandboxAppearance';

export function VaultSandboxAppearanceBridge() {
  const { resolvedTheme } = useTheme();
  const { i18n } = useTranslation();

  useLayoutEffect(() => {
    const appearance: VaultSandboxAppearance = {
      locale: normalizeVaultSandboxLocale(i18n.language),
      theme: resolvedTheme,
    };
    setVaultSandboxAppearance(appearance);
    getActiveVaultSandboxClient()?.setAppearance(appearance);
  }, [i18n.language, resolvedTheme]);

  return null;
}

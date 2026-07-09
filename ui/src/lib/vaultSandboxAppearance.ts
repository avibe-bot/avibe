export type VaultSandboxLocale = 'en' | 'zh';
export type VaultSandboxTheme = 'light' | 'dark';
export type VaultSandboxAppearance = {
  locale: VaultSandboxLocale;
  theme: VaultSandboxTheme;
};

const DEFAULT_APPEARANCE: VaultSandboxAppearance = { locale: 'en', theme: 'dark' };

let currentAppearance = DEFAULT_APPEARANCE;
const listeners = new Set<(appearance: VaultSandboxAppearance) => void>();

export function normalizeVaultSandboxLocale(language: string | undefined | null): VaultSandboxLocale {
  return language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

export function getVaultSandboxAppearance(): VaultSandboxAppearance {
  return currentAppearance;
}

export function setVaultSandboxAppearance(appearance: VaultSandboxAppearance): boolean {
  if (appearance.locale === currentAppearance.locale && appearance.theme === currentAppearance.theme) {
    return false;
  }
  currentAppearance = appearance;
  listeners.forEach((listener) => listener(currentAppearance));
  return true;
}

export function subscribeVaultSandboxAppearance(listener: (appearance: VaultSandboxAppearance) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

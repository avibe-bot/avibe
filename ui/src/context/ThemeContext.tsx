import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

export type ThemeMode = 'system' | 'light' | 'dark';
type ResolvedTheme = 'light' | 'dark';

type ThemeContextValue = {
  mode: ThemeMode;
  resolvedTheme: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
  cycleMode: () => void;
};

const STORAGE_KEY = 'vibe-remote-theme';
const VALID_MODES: ThemeMode[] = ['system', 'light', 'dark'];
const DEFAULT_MODE: ThemeMode = 'system';

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined);

function getSystemTheme(): ResolvedTheme {
  if (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: light)').matches
  ) {
    return 'light';
  }

  return 'dark';
}

function resolveTheme(mode: ThemeMode): ResolvedTheme {
  if (mode !== 'system') {
    return mode;
  }

  return getSystemTheme();
}

function applyTheme(mode: ThemeMode) {
  if (typeof document === 'undefined') {
    return;
  }

  if (mode === 'system') {
    document.documentElement.removeAttribute('data-theme');
    return;
  }

  document.documentElement.setAttribute('data-theme', mode);
}

function readStoredTheme(): ThemeMode {
  try {
    const queryMode = new URLSearchParams(window.location.search).get('theme');
    if (queryMode && VALID_MODES.includes(queryMode as ThemeMode)) {
      return queryMode as ThemeMode;
    }

    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value && VALID_MODES.includes(value as ThemeMode)) {
      return value as ThemeMode;
    }
  } catch {
    // Ignore storage issues and fall back to following the system preference.
  }

  return DEFAULT_MODE;
}

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [mode, setModeState] = useState<ThemeMode>(() => readStoredTheme());
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(() => resolveTheme(DEFAULT_MODE));
  const resolvedTheme = mode === 'system' ? systemTheme : mode;

  useEffect(() => {
    applyTheme(mode);
  }, [mode]);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') {
      return;
    }

    const mediaQuery = window.matchMedia('(prefers-color-scheme: light)');
    const handleChange = () => {
      setSystemTheme(resolveTheme(DEFAULT_MODE));
    };

    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange);
      return () => mediaQuery.removeEventListener('change', handleChange);
    }

    mediaQuery.addListener(handleChange);
    return () => mediaQuery.removeListener(handleChange);
  }, []);

  // useCallback so the exposed functions keep a stable identity, letting the
  // memoized value below change only on an actual theme change. Deps are all
  // stable (state setters + module-level helpers), so setMode never changes.
  const setMode = useCallback((nextMode: ThemeMode) => {
    setModeState(nextMode);
    setSystemTheme(resolveTheme(DEFAULT_MODE));

    try {
      // Persist all modes including "system" so the choice survives reloads;
      // removing the key would let readStoredTheme() fall back to the default
      // mode and silently drop system-follow behavior.
      window.localStorage.setItem(STORAGE_KEY, nextMode);
    } catch {
      // Ignore storage issues.
    }

    applyTheme(nextMode);
  }, []);

  const cycleMode = useCallback(() => {
    const nextMode: ThemeMode = mode === 'system' ? 'light' : mode === 'light' ? 'dark' : 'system';
    setMode(nextMode);
  }, [mode, setMode]);

  // Stable value identity (functions are useCallback-stable) so consumers only
  // re-render when the resolved theme actually changes.
  const value = useMemo(
    () => ({ mode, resolvedTheme, setMode, cycleMode }),
    [mode, resolvedTheme, setMode, cycleMode],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useTheme must be used within ThemeProvider');
  }
  return context;
}

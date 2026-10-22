import React, { createContext, useContext, useEffect, useState } from 'react';

const THEME_KEY = 'aidl_theme';
const PREFS_KEY = 'aidl_reader_prefs';

const DEFAULT_PREFS = {
  fontSize: 19, // 14 to 22
  fontFamily: 'serif', // serif | sans | mono
  lineHeight: 'relaxed', // normal (1.5) | relaxed (1.75) | loose (2.0)
  columnWidth: 'standard', // compact (560px) | standard (720px) | wide (880px) | full (100%)
};

const ThemeContext = createContext({
  theme: 'obsidian',
  setTheme: () => {},
  readerPrefs: DEFAULT_PREFS,
  setReaderPrefs: () => {},
});

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(() => {
    try {
      return localStorage.getItem(THEME_KEY) || 'obsidian';
    } catch {
      return 'obsidian';
    }
  });

  const [readerPrefs, setReaderPrefsState] = useState(() => {
    try {
      const stored = localStorage.getItem(PREFS_KEY);
      return stored ? { ...DEFAULT_PREFS, ...JSON.parse(stored) } : DEFAULT_PREFS;
    } catch {
      return DEFAULT_PREFS;
    }
  });

  const setTheme = (newTheme) => {
    setThemeState(newTheme);
    try {
      localStorage.setItem(THEME_KEY, newTheme);
    } catch { /* ignore */ }
  };

  const setReaderPrefs = (updater) => {
    setReaderPrefsState((prev) => {
      const updated = typeof updater === 'function' ? updater(prev) : { ...prev, ...updater };
      try {
        localStorage.setItem(PREFS_KEY, JSON.stringify(updated));
      } catch { /* ignore */ }
      return updated;
    });
  };

  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute('data-theme', theme);
    if (theme === 'parchment') {
      root.classList.add('theme-parchment');
      root.classList.remove('theme-obsidian', 'theme-oled');
    } else if (theme === 'oled') {
      root.classList.add('theme-oled');
      root.classList.remove('theme-obsidian', 'theme-parchment');
    } else {
      root.classList.add('theme-obsidian');
      root.classList.remove('theme-parchment', 'theme-oled');
    }
  }, [theme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, readerPrefs, setReaderPrefs }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}

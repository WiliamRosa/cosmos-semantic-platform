"use client";

// Light, dark or the system's choice. The class on <html> is set before the first paint by
// THEME_SCRIPT (layout.tsx), so a dark page never flashes white; this hook keeps it in step.

import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";
const STORAGE = "cosmos.theme";

/** Runs in <head> before React: applies the saved theme, or the system's, to <html>. */
export const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem("${STORAGE}");var d=t==="dark"||((!t||t==="system")&&matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.classList.toggle("dark",d);}catch(e){}})();`;

function apply(theme: Theme) {
  const dark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

interface ThemeContext {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const Context = createContext<ThemeContext | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("system");

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE);
      if (saved === "light" || saved === "dark" || saved === "system") setThemeState(saved);
    } catch {
      /* storage unavailable: follow the system */
    }
  }, []);

  useEffect(() => {
    apply(theme);
    if (theme !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => apply("system");
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE, next);
    } catch {
      /* not remembered */
    }
  }, []);

  return <Context.Provider value={{ theme, setTheme }}>{children}</Context.Provider>;
}

export function useTheme(): ThemeContext {
  const value = useContext(Context);
  if (!value) throw new Error("useTheme outside ThemeProvider");
  return value;
}

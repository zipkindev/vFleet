import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

export const PALETTES = ["forest", "midnight", "slate", "ocean", "ember", "paper", "contrast", "daylight"] as const;
export type Palette = (typeof PALETTES)[number];

export const VOICES = ["outfit", "inter", "compact", "rounded", "mono"] as const;
export type Voice = (typeof VOICES)[number];

export const PALETTE_LABELS: Record<Palette, string> = {
  forest:   "Forest",
  midnight: "Midnight",
  slate:    "Slate",
  ocean:    "Ocean",
  ember:    "Ember",
  paper:    "Paper",
  contrast: "High contrast",
  daylight: "Daylight",
};

export const VOICE_LABELS: Record<Voice, string> = {
  outfit:  "Outfit",
  inter:   "Inter",
  compact: "Compact",
  rounded: "Rounded",
  mono:    "Terminal",
};

export interface Preset {
  id: string;
  label: string;
  palette: Palette;
  voice: Voice;
}

export const PRESETS: Preset[] = [
  { id: "forest",    label: "Forest",        palette: "forest",   voice: "outfit"  },
  { id: "midnight",  label: "Midnight",       palette: "midnight", voice: "inter"   },
  { id: "slate",     label: "Slate",          palette: "slate",    voice: "compact" },
  { id: "ocean",     label: "Ocean",          palette: "ocean",    voice: "inter"   },
  { id: "ember",     label: "Ember",          palette: "ember",    voice: "outfit"  },
  { id: "paper",     label: "Paper",          palette: "paper",    voice: "inter"   },
  { id: "contrast",  label: "High contrast",  palette: "contrast", voice: "compact" },
  { id: "daylight",  label: "Daylight",        palette: "daylight", voice: "inter"   },
];

export interface ThemeState {
  palette: Palette;
  voice: Voice;
}

interface ThemeCtx {
  theme: ThemeState;
  setTheme: (next: ThemeState) => void;
  presetId: () => string;
}

const LS_KEY = "vfleet.theme";

function load(): ThemeState {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ThemeState>;
      const palette = PALETTES.includes(parsed.palette as Palette) ? (parsed.palette as Palette) : "forest";
      const voice   = VOICES.includes(parsed.voice as Voice) ? (parsed.voice as Voice) : "outfit";
      return { palette, voice };
    }
  } catch { /* ignore */ }
  return { palette: "forest", voice: "outfit" };
}

function applyTheme(theme: ThemeState) {
  document.documentElement.dataset.palette = theme.palette;
  document.documentElement.dataset.voice   = theme.voice;
}

const ThemeContext = createContext<ThemeCtx>({
  theme: { palette: "forest", voice: "outfit" },
  setTheme: () => undefined,
  presetId: () => "forest",
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<ThemeState>(load);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: ThemeState) => {
    setThemeState(next);
    try { localStorage.setItem(LS_KEY, JSON.stringify(next)); } catch { /* quota */ }
    applyTheme(next);
  }, []);

  const presetId = useCallback(() => {
    const hit = PRESETS.find((p) => p.palette === theme.palette && p.voice === theme.voice);
    return hit ? hit.id : "custom";
  }, [theme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, presetId }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}

import {
  PALETTE_LABELS,
  PALETTES,
  PRESETS,
  VOICE_LABELS,
  VOICES,
  useTheme,
  type Palette,
  type Voice,
} from "./ThemeContext";

export function ThemePanel({ onClose, inline }: { onClose: () => void; inline?: boolean }) {
  const { theme, setTheme, presetId } = useTheme();

  function applyPreset(id: string) {
    const preset = PRESETS.find((p) => p.id === id);
    if (preset) setTheme({ palette: preset.palette, voice: preset.voice });
  }

  const content = (
    <>
      <section className="theme-section">
        <p className="theme-label">Linked look</p>
        <div className="theme-presets">
          {PRESETS.map((preset) => (
            <button
              key={preset.id}
              className={`theme-preset-btn${presetId() === preset.id ? " active" : ""}`}
              onClick={() => applyPreset(preset.id)}
            >
              <span className="theme-preset-swatch" data-palette={preset.palette} />
              {preset.label}
            </button>
          ))}
        </div>
      </section>

      <section className="theme-section">
        <p className="theme-label">Color palette</p>
        <div className="theme-grid">
          {PALETTES.map((id) => (
            <button
              key={id}
              className={`theme-swatch-btn${theme.palette === id ? " active" : ""}`}
              title={PALETTE_LABELS[id]}
              onClick={() => setTheme({ ...theme, palette: id as Palette })}
            >
              <span className="theme-swatch" data-palette={id} />
              <span className="theme-swatch-label">{PALETTE_LABELS[id]}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="theme-section">
        <p className="theme-label">Text style</p>
        <div className="theme-voices">
          {VOICES.map((id) => (
            <button
              key={id}
              className={`theme-voice-btn${theme.voice === id ? " active" : ""}`}
              onClick={() => setTheme({ ...theme, voice: id as Voice })}
            >
              {VOICE_LABELS[id]}
            </button>
          ))}
        </div>
      </section>

      <p className="theme-hint">Saved in this browser only.</p>
    </>
  );

  if (inline) {
    return <div className="theme-panel-inline">{content}</div>;
  }

  return (
    <div className="theme-panel" role="dialog" aria-label="Theme settings">
      <div className="theme-panel-header">
        <span className="theme-panel-title">Appearance</span>
        <button className="theme-panel-close" onClick={onClose} aria-label="Close theme settings">
          ✕
        </button>
      </div>
      {content}
    </div>
  );
}

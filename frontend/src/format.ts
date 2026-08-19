export function gib(mib: number): string {
  if (mib < 1024) return `${mib} MiB`;
  return `${(mib / 1024).toFixed(mib >= 10240 ? 0 : 1)} GiB`;
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KiB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MiB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(value >= 10 * 1024 * 1024 * 1024 ? 0 : 1)} GiB`;
}

export function relTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const delta = Date.now() - then;
  const days = delta / 86400000;
  if (days < 1 / 24) return `${Math.max(1, Math.round(delta / 60000))}m ago`;
  if (days < 1) return `${Math.round(days * 24)}h ago`;
  if (days < 45) return `${Math.round(days)}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function powerLabel(state: string): string {
  const normalized = state.replace(/_/g, "").toLowerCase();
  if (normalized === "poweredon") return "On";
  if (normalized === "poweredoff") return "Off";
  if (normalized === "suspended") return "Suspended";
  return state;
}

export function diskLabel(kind: string): string {
  if (kind === "thin") return "Thin";
  if (kind === "thick") return "Thick";
  if (kind === "mixed") return "Mixed";
  return "—";
}

export function storageGib(value: number): number {
  return value / (1024 * 1024 * 1024);
}

export function powerClass(state: string): string {
  const normalized = state.replace(/_/g, "").toLowerCase();
  if (normalized === "poweredon") return "POWERED_ON";
  if (normalized === "poweredoff") return "POWERED_OFF";
  if (normalized === "suspended") return "SUSPENDED";
  return state;
}

export function csvEscape(value: string): string {
  if (/[",\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`;
  return value;
}

export function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

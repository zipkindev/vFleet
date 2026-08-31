import { useEffect, useState } from "react";
import { listVmFolders } from "./api";
import type { VmFolder } from "./types";

type Props = {
  value: string;
  onChange: (folderId: string) => void;
  defaultLabel?: string;
};

export function FolderPicker({ value, onChange, defaultLabel = "Same as template / source (default)" }: Props) {
  const [folders, setFolders] = useState<VmFolder[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listVmFolders()
      .then((rows) => {
        if (!cancelled) {
          setFolders(rows);
          setError("");
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setFolders([]);
          setError(err instanceof Error ? err.message : "Could not load folders");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <label>
      Destination folder
      <select value={value} onChange={(event) => onChange(event.target.value)} disabled={loading}>
        <option value="">{loading ? "Loading folders…" : defaultLabel}</option>
        {folders.map((item) => (
          <option key={item.id} value={item.id}>
            {item.path}
          </option>
        ))}
      </select>
      {error ? <span className="empty">{error}</span> : null}
    </label>
  );
}

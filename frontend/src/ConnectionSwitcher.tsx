import { useMemo, useState } from "react";
import { relTime } from "./format";
import type { ConnectionInfo, ConnectionProfile, ConnectionProfileList } from "./types";

type Props = {
  connection: ConnectionInfo | null;
  profiles: ConnectionProfileList | null;
  loading: boolean;
  error: string;
  onSwitch: (profile: ConnectionProfile) => Promise<void>;
  onAdd: () => void;
  onEdit: (profile: ConnectionProfile) => void;
  onRemove: (profile: ConnectionProfile) => Promise<void>;
};

function endpointLabel(kind: string): string {
  if (kind === "vcenter") return "vCenter";
  if (kind === "esxi") return "Direct ESXi";
  return "vSphere";
}

export function ConnectionSwitcher({
  connection,
  profiles,
  loading,
  error,
  onSwitch,
  onAdd,
  onEdit,
  onRemove,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [workingId, setWorkingId] = useState("");
  const tone = connection?.stale ? "warm" : connection?.connected ? "ok" : connection ? "bad" : "muted";
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = profiles?.profiles ?? [];
    if (!needle) return all;
    return all.filter((profile) =>
      [profile.name, profile.host, profile.user, endpointLabel(profile.endpoint_kind)]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [profiles, query]);

  async function switchTo(profile: ConnectionProfile) {
    if (profile.active || workingId) return;
    setWorkingId(profile.id);
    try {
      await onSwitch(profile);
      setOpen(false);
      setQuery("");
    } catch {
      /* The parent keeps the drawer open and displays the scoped API error. */
    } finally {
      setWorkingId("");
    }
  }

  async function remove(profile: ConnectionProfile) {
    if (workingId) return;
    if (!window.confirm(`Remove the saved connection “${profile.name}”? This deletes its locally retained credentials.`)) return;
    setWorkingId(profile.id);
    try {
      await onRemove(profile);
    } catch {
      /* The parent surfaces removal errors in this drawer. */
    } finally {
      setWorkingId("");
    }
  }

  return (
    <div className="connection-switcher">
      <button
        className={`pill connection-selector-trigger ${tone}`}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <span>
          {connection?.mode === "esxi" ? "Direct ESXi" : connection?.mode === "vcenter" ? "vCenter" : "Connections"}
          <b>{profiles?.profiles.length ?? 0}</b>
        </span>
        <small>{connection?.host || "Choose a saved endpoint"}</small>
        <small>{connection?.user || "Add vCenter or ESXi"}</small>
        <i aria-hidden="true">›</i>
      </button>

      {open ? (
        <div className="connection-drawer-backdrop" onMouseDown={() => !workingId && setOpen(false)}>
          <aside
            className="connection-drawer"
            role="dialog"
            aria-label="Saved vSphere connections"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header>
              <div>
                <strong>Connections</strong>
                <small>{profiles?.profiles.length ?? 0} saved locally</small>
              </div>
              <button className="drawer-close" onClick={() => setOpen(false)} aria-label="Close connections">✕</button>
            </header>
            <input
              className="connection-search"
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter name, host, or user…"
              aria-label="Filter saved connections"
            />
            {error ? <div className="banner bad">{error}</div> : null}
            <div className="connection-list">
              {rows.map((profile) => (
                <article className={`connection-profile${profile.active ? " active" : ""}`} key={profile.id}>
                  <button
                    className="connection-profile-main"
                    disabled={profile.active || Boolean(workingId)}
                    onClick={() => void switchTo(profile)}
                  >
                    <span>
                      <strong>{profile.name}</strong>
                      <em>{endpointLabel(profile.endpoint_kind)}</em>
                    </span>
                    <small>{profile.host}{profile.port !== 443 ? `:${profile.port}` : ""}</small>
                    <small>{profile.user}</small>
                    {profile.last_used_at ? <small>used {relTime(profile.last_used_at)}</small> : null}
                    <b>{profile.active ? "Connected" : workingId === profile.id ? "Connecting…" : "Connect"}</b>
                  </button>
                  <div className="connection-profile-actions">
                    <button onClick={() => { setOpen(false); onEdit(profile); }} disabled={Boolean(workingId)}>Edit</button>
                    <button className="danger" onClick={() => void remove(profile)} disabled={profile.active || Boolean(workingId)}>
                      Remove
                    </button>
                  </div>
                </article>
              ))}
              {!loading && rows.length === 0 ? (
                <p className="empty">{query ? "No saved connections match that filter." : "No connections saved yet."}</p>
              ) : null}
              {loading ? <p className="empty">Loading connections…</p> : null}
            </div>
            <footer>
              <button className="accent wide" onClick={() => { setOpen(false); onAdd(); }}>
                Add connection
              </button>
              <small>Credentials stay in an owner-only, gitignored file on this machine.</small>
            </footer>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

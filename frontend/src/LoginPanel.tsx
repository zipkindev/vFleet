import { useState } from "react";
import { login } from "./api";
import type { ConnectionInfo, ConnectionProfile } from "./types";

type Props = {
  connection: ConnectionInfo | null;
  profile?: ConnectionProfile | null;
  blank?: boolean;
  onClose: () => void;
  onConnected: (info: ConnectionInfo) => void;
};

export function LoginPanel({ connection, profile, blank = false, onClose, onConnected }: Props) {
  const fallback = blank ? null : connection;
  const [profileName, setProfileName] = useState(profile?.name || "");
  const [host, setHost] = useState(profile?.host || fallback?.saved_host || "");
  const [user, setUser] = useState(profile?.user || fallback?.saved_user || "");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState(String(profile?.port || fallback?.saved_port || 443));
  const [insecure, setInsecure] = useState(profile?.insecure ?? fallback?.insecure ?? true);
  const [sshEnabled, setSshEnabled] = useState(profile?.ssh_enabled ?? fallback?.ssh_configured ?? false);
  const [sshUser, setSshUser] = useState(profile?.ssh_user || fallback?.ssh_user || "root");
  const [sshPassword, setSshPassword] = useState("");
  const [sshPort, setSshPort] = useState(String(profile?.ssh_port || fallback?.ssh_port || 22));
  const [sshFingerprint, setSshFingerprint] = useState(profile?.ssh_host_key_sha256 || fallback?.ssh_host_key_sha256 || "");
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const savedPassword = Boolean(
    (profile?.has_saved_password &&
      host.trim() === profile.host &&
      user.trim() === profile.user &&
      (Number(port) || 443) === profile.port) ||
      (fallback?.has_saved_password &&
        host.trim() === (fallback?.saved_host || "") &&
        user.trim() === (fallback?.saved_user || "") &&
        (Number(port) || 443) === (fallback?.saved_port || 443))
  );
  const savedSshPassword = Boolean(
    (profile?.has_saved_ssh_password &&
      host.trim() === profile.host &&
      sshUser.trim() === profile.ssh_user &&
      (Number(sshPort) || 22) === profile.ssh_port) ||
      (fallback?.has_saved_ssh_password &&
        host.trim() === (fallback?.saved_host || "") &&
        sshUser.trim() === (fallback?.ssh_user || "") &&
        (Number(sshPort) || 22) === (fallback?.ssh_port || 22))
  );

  async function submit(kind: "test" | "save") {
    if (!host.trim() || !user.trim()) {
      setError("Host, username, and password are required");
      return;
    }
    if (!password && !savedPassword) {
      setError("Host, username, and password are required");
      return;
    }
    if (sshEnabled && (!sshUser.trim() || !sshFingerprint.trim())) {
      setError("SSH user and host key fingerprint are required when SSH fallback is enabled");
      return;
    }
    setBusy(kind);
    setError("");
    setOk("");
    try {
      const info = await login({
        host,
        user,
        password,
        port: Number(port) || 443,
        insecure,
        remember: kind === "save",
        connect: kind === "save",
        endpoint_kind: "auto",
        ssh_enabled: sshEnabled,
        ssh_user: sshUser,
        ssh_password: sshPassword,
        ssh_port: Number(sshPort) || 22,
        ssh_host_key_sha256: sshFingerprint,
        profile_id: profile?.id,
        profile_name: profileName.trim() || host.trim(),
      });
      if (kind === "save") {
        onConnected(info);
      } else {
        setOk(info.message || "Credentials work. Password is saved on this machine.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="modal-back" onClick={onClose}>
      <form
        className="modal login-modal"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          void submit("save");
        }}
      >
        <h2>{profile ? "Edit connection" : "Add vSphere connection"}</h2>
        <p>
          Enter either a vCenter Server or a standalone ESXi host. vFleet detects which one it is.
          Test does not save or switch connections; Connect retains this profile locally and makes it active.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {ok ? <div className="banner ok">{ok}</div> : null}
        <label>
          Display name
          <input
            value={profileName}
            onChange={(event) => setProfileName(event.target.value)}
            placeholder="Production vCenter or Lab ESXi"
            autoComplete="off"
            name="profile-name"
          />
        </label>
        <label>
          Server
          <input
            autoFocus
            value={host}
            onChange={(event) => setHost(event.target.value)}
            placeholder="vcenter.example.com or 192.168.1.20"
            autoComplete="off"
            name="vcenter-host"
          />
        </label>
        <div className="login-grid">
          <label>
            Username
            <input
              value={user}
              onChange={(event) => setUser(event.target.value)}
              placeholder="user@vsphere.local or root"
              autoComplete="off"
              name="vcenter-user"
            />
          </label>
          <label>
            Port
            <input value={port} onChange={(event) => setPort(event.target.value)} inputMode="numeric" name="vcenter-port" />
          </label>
        </div>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="off"
            name="vcenter-password"
            placeholder={savedPassword && !password ? "Saved on this machine" : ""}
          />
        </label>
        <label className="check">
          <input type="checkbox" checked={insecure} onChange={(event) => setInsecure(event.target.checked)} />
          Trust self-signed certificate
        </label>
        <details className="advanced-box">
          <summary>Standalone ESXi SSH fallback (optional)</summary>
          <p>
            Used only for explicitly planned disk conversions when the licensed vSphere API cannot write. Host-key
            verification is required; arbitrary shell commands are never accepted from the UI.
          </p>
          <label className="check">
            <input type="checkbox" checked={sshEnabled} onChange={(event) => setSshEnabled(event.target.checked)} />
            Enable verified SSH fallback
          </label>
          {sshEnabled ? (
            <>
              <div className="login-grid">
                <label>SSH user<input value={sshUser} onChange={(event) => setSshUser(event.target.value)} /></label>
                <label>SSH port<input value={sshPort} onChange={(event) => setSshPort(event.target.value)} inputMode="numeric" /></label>
              </div>
              <label>
                SSH password
                <input
                  type="password"
                  value={sshPassword}
                  onChange={(event) => setSshPassword(event.target.value)}
                  autoComplete="off"
                  placeholder={savedSshPassword && !sshPassword ? "Saved on this machine" : ""}
                />
              </label>
              <label>Host key fingerprint<input value={sshFingerprint} onChange={(event) => setSshFingerprint(event.target.value)} placeholder="SHA256:…" /></label>
            </>
          ) : null}
        </details>
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy !== null}>
            Cancel
          </button>
          <button type="button" className="test-btn" onClick={() => void submit("test")} disabled={busy !== null}>
            {busy === "test" ? "Testing…" : "Test"}
          </button>
          <button type="submit" className="accent" disabled={busy !== null}>
            {busy === "save" ? "Connecting…" : "Connect & save"}
          </button>
        </div>
      </form>
    </div>
  );
}

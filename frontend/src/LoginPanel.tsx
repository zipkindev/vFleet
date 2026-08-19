import { useState } from "react";
import { login } from "./api";
import type { ConnectionInfo } from "./types";

type Props = {
  connection: ConnectionInfo | null;
  onClose: () => void;
  onConnected: (info: ConnectionInfo) => void;
};

export function LoginPanel({ connection, onClose, onConnected }: Props) {
  const [host, setHost] = useState(connection?.saved_host || "");
  const [user, setUser] = useState(connection?.saved_user || "");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState(String(connection?.saved_port || 443));
  const [insecure, setInsecure] = useState(connection?.insecure ?? true);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const savedPassword = Boolean(
    connection?.has_saved_password &&
      host.trim() === (connection?.saved_host || "") &&
      user.trim() === (connection?.saved_user || "") &&
      (Number(port) || 443) === (connection?.saved_port || 443)
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
        remember: true,
        connect: true,
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
        <h2>Connect to vCenter</h2>
        <p>
          Test verifies the account and keeps the password. Save stores the endpoint in local{" "}
          <code>.env</code> and switches to live inventory. A saved password stays on this machine
          and does not need to be retyped.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {ok ? <div className="banner ok">{ok}</div> : null}
        <label>
          Server
          <input
            autoFocus
            value={host}
            onChange={(event) => setHost(event.target.value)}
            placeholder="vcenter.example.com or https://vcenter:443"
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
              placeholder="user@vsphere.local"
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
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy !== null}>
            Stay in demo
          </button>
          <button type="button" className="test-btn" onClick={() => void submit("test")} disabled={busy !== null}>
            {busy === "test" ? "Testing…" : "Test"}
          </button>
          <button type="submit" className="accent" disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

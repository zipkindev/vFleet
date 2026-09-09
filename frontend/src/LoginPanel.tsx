import { useEffect, useMemo, useState } from "react";
import { fetchAutomationCredentials, fetchSshHostKey, login, saveAutomationCredential, testSshConnection } from "./api";
import type { AutomationCredential, ConnectionInfo, ConnectionProfile, JumpHostType } from "./types";

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
  const [allowSshStart, setAllowSshStart] = useState(false);
  const [sshUser, setSshUser] = useState(profile?.ssh_user || fallback?.ssh_user || "root");
  const [sshPassword, setSshPassword] = useState("");
  const [sshPort, setSshPort] = useState(String(profile?.ssh_port || fallback?.ssh_port || 22));
  const [sshFingerprint, setSshFingerprint] = useState(profile?.ssh_host_key_sha256 || fallback?.ssh_host_key_sha256 || "");
  const [jumpEnabled, setJumpEnabled] = useState(profile?.jump_enabled ?? false);
  const [jumpAddress, setJumpAddress] = useState(profile?.jump_address || "");
  const [jumpPort, setJumpPort] = useState(String(profile?.jump_port || 22));
  const [jumpHostType, setJumpHostType] = useState<JumpHostType>(profile?.jump_host_type || "auto");
  const [jumpCredentialId, setJumpCredentialId] = useState(profile?.jump_credential_id || "");
  const [newJumpCredential, setNewJumpCredential] = useState(false);
  const [jumpCredentialName, setJumpCredentialName] = useState("");
  const [jumpUsername, setJumpUsername] = useState("");
  const [jumpPassword, setJumpPassword] = useState("");
  const [jumpFingerprint, setJumpFingerprint] = useState(profile?.jump_host_key_sha256 || "");
  const [credentials, setCredentials] = useState<AutomationCredential[]>([]);
  const [busy, setBusy] = useState<"test" | "save" | "jump" | "credential" | "ssh-key" | null>(null);
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
  const jumpCredentials = useMemo(
    () => credentials.filter((credential) => credential.kind === "ssh" || credential.kind === "service"),
    [credentials],
  );

  useEffect(() => {
    let cancelled = false;
    void fetchAutomationCredentials({ profileId: profile?.id, globalOnly: blank }).then((result) => {
      if (!cancelled) setCredentials((current) => [...result.credentials, ...current.filter((item) => !result.credentials.some((loaded) => loaded.id === item.id))]);
    }).catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : "Could not load jump-host credentials"); });
    return () => { cancelled = true; };
  }, [blank, profile?.id]);

  function clearJumpReview() {
    setJumpFingerprint("");
    setOk("");
  }

  async function saveJumpCredential() {
    if (busy !== null) return;
    if (!jumpCredentialName.trim() || !jumpUsername.trim() || !jumpPassword) {
      setError("Enter a vault name, SSH username, and password");
      return;
    }
    setBusy("credential");
    setError("");
    setOk("");
    try {
      const saved = await saveAutomationCredential({
        name: jumpCredentialName.trim(),
        kind: "ssh",
        username: jumpUsername.trim(),
        secret: jumpPassword,
        scope: "global",
      });
      setCredentials((current) => [...current.filter((item) => item.id !== saved.id), saved]);
      setJumpCredentialId(saved.id);
      setJumpPassword("");
      setJumpUsername("");
      setJumpCredentialName("");
      setNewJumpCredential(false);
      setJumpFingerprint("");
      setOk("SSH credential saved to the vault and selected. Test the jump host to pin its key.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the SSH credential");
    } finally {
      setBusy(null);
    }
  }

  async function testJumpHost() {
    if (busy !== null) return;
    if (newJumpCredential || !jumpAddress.trim() || !jumpCredentialId) {
      setError("Enter a jump-host address and select a stored SSH credential");
      return;
    }
    setBusy("jump");
    setError("");
    setOk("");
    setJumpFingerprint("");
    try {
      const fingerprint = await fetchSshHostKey(jumpAddress.trim(), Number(jumpPort) || 22);
      const result = await testSshConnection({
        address: jumpAddress.trim(),
        port: Number(jumpPort) || 22,
        credential_id: jumpCredentialId,
        host_key_sha256: fingerprint,
        host_type: jumpHostType,
        profile_id: profile?.id,
      });
      setJumpFingerprint(fingerprint);
      const detected = result.detected_host_type === "auto" ? "type could not be discovered" : `${result.detected_host_type} host`;
      setOk(`Jump-host authentication passed (${detected}). Pinned ${fingerprint}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not authenticate to the SSH jump host");
    } finally {
      setBusy(null);
    }
  }

  async function detectEsxiHostKey() {
    let fingerprint: string;
    try {
      fingerprint = await fetchSshHostKey(host.trim(), Number(sshPort) || 22);
    } catch (err) {
      if (!jumpEnabled || !jumpAddress.trim() || !jumpCredentialId || newJumpCredential || !jumpFingerprint.startsWith("SHA256:")) throw err;
      fingerprint = await fetchSshHostKey(host.trim(), Number(sshPort) || 22, {
        address: jumpAddress.trim(),
        port: Number(jumpPort) || 22,
        credential_id: jumpCredentialId,
        host_key_sha256: jumpFingerprint,
      });
    }
    if (!fingerprint.startsWith("SHA256:")) {
      throw new Error("Could not detect the ESXi SSH host key. Check SSH access and try again.");
    }
    return fingerprint;
  }

  async function pinEsxiHostKey() {
    if (busy !== null) return;
    if (allowSshStart && !sshFingerprint.trim()) { await submit("test"); return; }
    if (!host.trim()) {
      setError("Enter the ESXi server address first");
      return;
    }
    setBusy("ssh-key");
    setError("");
    setOk("");
    try {
      const fingerprint = await detectEsxiHostKey();
      if (sshFingerprint.trim() && fingerprint !== sshFingerprint.trim()) {
        throw new Error("The ESXi SSH host key differs from the pinned key. Verify the server identity before changing the saved fingerprint.");
      }
      setSshFingerprint(fingerprint);
      setOk("ESXi SSH host key detected and pinned.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not detect the ESXi SSH host key");
    } finally {
      setBusy(null);
    }
  }

  async function submit(kind: "test" | "save") {
    if (busy !== null) return;
    if (!host.trim() || !user.trim()) {
      setError("Host, username, and password are required");
      return;
    }
    if (!password && !savedPassword) {
      setError("Host, username, and password are required");
      return;
    }
    if (sshEnabled && !sshUser.trim()) {
      setError("SSH user is required when SSH fallback is enabled");
      return;
    }
    if (jumpEnabled && (newJumpCredential || !jumpAddress.trim() || !jumpCredentialId || !jumpFingerprint.startsWith("SHA256:"))) {
      setError("Test the jump host and pin its host key before saving this access path");
      return;
    }
    setBusy(kind);
    setError("");
    setOk("");
    try {
      let fingerprint = sshFingerprint.trim();
      if (sshEnabled && !fingerprint) {
        setOk("Detecting the ESXi SSH host key…");
        try {
          fingerprint = await detectEsxiHostKey();
        } catch (err) {
          if (!allowSshStart) throw err;
          setOk("Testing vSphere access and temporarily starting ESXi SSH if needed…");
        }
        setSshFingerprint(fingerprint);
        setOk("");
      }
      const info = await login({
        host,
        user,
        password,
        port: Number(port) || 443,
        insecure,
        remember: kind === "save",
        connect: kind === "save",
        endpoint_kind: "auto",
        ssh_start_service_confirm: sshEnabled && allowSshStart && !fingerprint,
        ssh_enabled: sshEnabled,
        ssh_user: sshUser,
        ssh_password: sshPassword,
        ssh_port: Number(sshPort) || 22,
        ssh_host_key_sha256: fingerprint,
        jump_enabled: jumpEnabled,
        jump_address: jumpEnabled ? jumpAddress.trim() : "",
        jump_port: Number(jumpPort) || 22,
        jump_host_type: jumpEnabled ? jumpHostType : "auto",
        jump_credential_id: jumpEnabled ? jumpCredentialId : "",
        jump_host_key_sha256: jumpEnabled ? jumpFingerprint : "",
        profile_id: profile?.id,
        profile_name: profileName.trim() || host.trim(),
      });
      if (sshEnabled && info.ssh_host_key_sha256) setSshFingerprint(info.ssh_host_key_sha256);
      if (kind === "save") {
        onConnected(info);
      } else {
        setOk(info.message || "Credentials work. Password is saved on this machine.");
      }
    } catch (err) {
      setOk("");
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="modal-back" onClick={() => { if (busy === null) onClose(); }}>
      <form
        className="modal login-modal"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          void submit("save");
        }}
      >
        <fieldset className="login-fields" disabled={busy !== null}>
        <h2>{profile ? "Edit connection" : "Add vSphere connection"}</h2>
        <p>
          Enter either a vCenter Server or a standalone ESXi host. vFleet detects which one it is.
          Test does not save or switch connections; Connect encrypts this profile locally and makes it active.
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
            onChange={(event) => { setHost(event.target.value); setSshFingerprint(""); }}
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
        <details className="advanced-box" open={jumpEnabled}>
          <summary>Network access · SSH jump host (optional)</summary>
          <p>
            Saves a default one-hop route for SSH-based guest and standalone ESXi operations in this environment.
            VMware Tools deployments inherit it automatically; the vCenter HTTPS/SOAP connection remains direct.
          </p>
          <label className="check">
            <input type="checkbox" checked={jumpEnabled} onChange={(event) => { setJumpEnabled(event.target.checked); setOk(""); }} />
            Use an SSH jump host for secured networks
          </label>
          {jumpEnabled ? (
            <>
              <div className="login-grid">
                <label>Jump-host address<input value={jumpAddress} onChange={(event) => { setJumpAddress(event.target.value); clearJumpReview(); }} placeholder="access.example.com or 192.168.1.60" /></label>
                <label>SSH port<input value={jumpPort} onChange={(event) => { setJumpPort(event.target.value); clearJumpReview(); }} inputMode="numeric" /></label>
              </div>
              <label>
                Jump-host credential from vault
                <select value={newJumpCredential ? "__new__" : jumpCredentialId} onChange={(event) => {
                  const creating = event.target.value === "__new__";
                  setNewJumpCredential(creating);
                  if (!creating) setJumpCredentialId(event.target.value);
                  setJumpPassword("");
                  clearJumpReview();
                }}>
                  <option value="">Select stored SSH credential…</option>
                  <option value="__new__">Save a new SSH credential to vault…</option>
                  {jumpCredentials.map((credential) => <option key={credential.id} value={credential.id}>{credential.name} · {credential.username}</option>)}
                </select>
              </label>
              {newJumpCredential ? (
                <div className="jump-credential-editor">
                  <label>Vault name<input value={jumpCredentialName} onChange={(event) => setJumpCredentialName(event.target.value)} autoComplete="off" placeholder="Office jump host" /></label>
                  <label>Jump-host username<input value={jumpUsername} onChange={(event) => setJumpUsername(event.target.value)} autoComplete="off" /></label>
                  <label>Jump-host password<input type="password" value={jumpPassword} onChange={(event) => setJumpPassword(event.target.value)} autoComplete="new-password" /></label>
                  <p>Save this SSH credential encrypted in the Automation Vault, available to all endpoints. It stays in the vault even if you cancel connection setup.</p>
                  <button type="button" className="test-btn" disabled={busy !== null} onClick={() => void saveJumpCredential()}>
                    {busy === "credential" ? "Saving credential…" : "Save to vault & select"}
                  </button>
                </div>
              ) : null}
              <label>
                Jump-host type
                <select value={jumpHostType} onChange={(event) => { setJumpHostType(event.target.value as JumpHostType); clearJumpReview(); }}>
                  <option value="auto">Auto detect</option>
                  <option value="windows">Windows OpenSSH (PowerShell)</option>
                  <option value="unix">Linux or Unix</option>
                </select>
              </label>
              {!jumpCredentials.length ? <small>Choose “Save a new SSH credential to vault…” above to add your first credential here.</small> : null}
              {jumpFingerprint ? <small className="host-key">Pinned jump-host key: {jumpFingerprint}</small> : null}
              <button type="button" className="test-btn" disabled={busy !== null || newJumpCredential || !jumpCredentialId} onClick={() => void testJumpHost()}>
                {busy === "jump" ? "Testing jump host…" : "Test jump host & pin key"}
              </button>
            </>
          ) : null}
        </details>
        <details className="advanced-box">
          <summary>Standalone ESXi SSH fallback (optional)</summary>
          <p>
            Used only for explicitly planned disk conversions when the licensed vSphere API cannot write. Host-key
            verification uses the pinned key; arbitrary shell commands are never accepted from the UI.
          </p>
          <label className="check">
            <input type="checkbox" checked={sshEnabled} onChange={(event) => setSshEnabled(event.target.checked)} />
            Enable verified SSH fallback
          </label>
          {sshEnabled ? (
            <>
              <div className="login-grid">
                <label>SSH user<input value={sshUser} onChange={(event) => setSshUser(event.target.value)} /></label>
                <label>SSH port<input value={sshPort} onChange={(event) => { setSshPort(event.target.value); setSshFingerprint(""); }} inputMode="numeric" /></label>
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
              <label className="check">
                <input type="checkbox" checked={allowSshStart} onChange={(event) => setAllowSshStart(event.target.checked)} />
                Allow temporary SSH start through the vSphere API
              </label>
              <small>If key detection fails, use the supplied vSphere and SSH credentials to start SSH only if stopped, test SSH authentication, pin its key, and restore its previous service state.</small>
              <button type="button" className="test-btn" disabled={busy !== null} onClick={() => void pinEsxiHostKey()}>
                {busy === "ssh-key" ? "Detecting ESXi SSH key…" : "Detect & pin ESXi SSH key"}
              </button>
              <label>Host key fingerprint (optional)<input value={sshFingerprint} onChange={(event) => setSshFingerprint(event.target.value)} placeholder="Detected automatically when left blank" /></label>
              <small>Test or Connect &amp; save detects and pins the first SSH key when this is blank, trying direct access first and then your verified jump host if needed. Enter a known SHA256 fingerprint to pin that key instead.</small>
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
        </fieldset>
      </form>
    </div>
  );
}

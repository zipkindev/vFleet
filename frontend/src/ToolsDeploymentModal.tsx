import { useEffect, useMemo, useState } from "react";
import { deployGuestTools, fetchAutomationCredentials, fetchSshHostKey, saveAutomationCredential } from "./api";
import type { AutomationCredential, Job, ToolsDeploymentTarget, VirtualMachine } from "./types";

type Props = {
  vms: VirtualMachine[];
  onClose: () => void;
  onQueued: (jobs: Job[], failures: string[], vmIds: string[]) => void;
};

type TargetRow = ToolsDeploymentTarget & { name: string; error: string };

function guestFamily(guestOs: string): "windows" | "linux" {
  return /win/i.test(guestOs) ? "windows" : "linux";
}

export function ToolsDeploymentModal({ vms, onClose, onQueued }: Props) {
  const [targets, setTargets] = useState<TargetRow[]>(() => vms.map((vm) => ({
    vm_id: vm.id,
    name: vm.name,
    address: vm.ip_address || "",
    os_family: guestFamily(vm.guest_os),
    ssh_host_key_sha256: "",
    error: "",
  })));
  const [credentials, setCredentials] = useState<AutomationCredential[]>([]);
  const [credentialId, setCredentialId] = useState("");
  const [newCredential, setNewCredential] = useState(false);
  const [credentialName, setCredentialName] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [credentialScope, setCredentialScope] = useState<"global" | "endpoint">("endpoint");
  const [windowsTransport, setWindowsTransport] = useState<"http" | "https">("http");
  const [windowsPort, setWindowsPort] = useState(5985);
  const [validateCertificate, setValidateCertificate] = useState(true);
  const [linuxPort, setLinuxPort] = useState(22);
  const [sudo, setSudo] = useState(true);
  const [reviewed, setReviewed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const families = useMemo(() => new Set(targets.map((target) => target.os_family)), [targets]);
  const requiredKind = families.size > 1 ? "service" : families.has("windows") ? "windows" : "ssh";
  const compatible = credentials.filter((item) => item.kind === requiredKind || item.kind === "service");

  useEffect(() => {
    void fetchAutomationCredentials().then((result) => {
      setCredentials(result.credentials);
      const matching = result.credentials.find((item) => item.kind === requiredKind || item.kind === "service");
      if (matching) setCredentialId(matching.id);
      setNewCredential(!matching);
    }).catch((err) => setError(err instanceof Error ? err.message : "Could not load Automation Vault credentials"));
  }, [requiredKind]);

  function updateTarget(index: number, update: Partial<TargetRow>) {
    setTargets((current) => current.map((target, position) => position === index ? { ...target, ...update, ssh_host_key_sha256: update.address !== undefined || update.os_family !== undefined ? "" : target.ssh_host_key_sha256 } : target));
    setReviewed(false);
  }

  async function review() {
    setBusy(true);
    setError("");
    const next = targets.map((target) => ({ ...target, error: "" }));
    for (const target of next) {
      if (!target.address.trim()) {
        target.error = "Enter an IP address or DNS name";
        continue;
      }
      if (target.os_family === "linux") {
        try {
          target.ssh_host_key_sha256 = await fetchSshHostKey(target.address.trim(), linuxPort);
        } catch (err) {
          target.error = err instanceof Error ? err.message : "Could not read the SSH host key";
        }
      }
    }
    setTargets(next);
    const invalidCredential = newCredential
      ? !credentialName.trim() || !username.trim() || !secret
      : !credentialId;
    if (invalidCredential) setError(newCredential ? "Name, username, and password are required for the new credential" : "Select a stored credential");
    setReviewed(!invalidCredential && next.every((target) => !target.error));
    setBusy(false);
  }

  async function queue() {
    setBusy(true);
    setError("");
    try {
      let selectedId = credentialId;
      if (newCredential) {
        const saved = await saveAutomationCredential({
          name: credentialName.trim(),
          kind: requiredKind,
          username: username.trim(),
          secret,
          scope: credentialScope,
        });
        selectedId = saved.id;
      }
      const result = await deployGuestTools({
        targets: targets.map(({ vm_id, address, os_family, ssh_host_key_sha256 }) => ({ vm_id, address: address.trim(), os_family, ssh_host_key_sha256 })),
        credential_id: selectedId,
        windows_transport: windowsTransport,
        windows_port: windowsPort,
        validate_certificate: validateCertificate,
        linux_port: linuxPort,
        sudo,
      });
      if (!result.jobs.length) throw new Error(result.failures.join("; ") || "No Tools deployment jobs were queued");
      onQueued(result.jobs, result.failures, targets.map((target) => target.vm_id));
    } catch (err) {
      setConfirming(false);
      setError(err instanceof Error ? err.message : "Could not queue VMware Tools deployment");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="modal-back" onClick={() => !busy && onClose()}>
        <div className="modal tools-modal" onClick={(event) => event.stopPropagation()}>
          <h2>Deploy VMware Tools · {vms.length} VM{vms.length === 1 ? "" : "s"}</h2>
          <p>Windows uses WinRM and the ESXi-hosted Tools installer. Linux uses pinned-key SSH and the distribution’s <code>open-vm-tools</code> package. Every VM becomes an independent persistent job.</p>
          {error ? <div className="banner bad">{error}</div> : null}
          <div className="tools-settings-grid">
            <label>Credential<select value={newCredential ? "new" : credentialId} onChange={(event) => { setNewCredential(event.target.value === "new"); setCredentialId(event.target.value === "new" ? "" : event.target.value); setReviewed(false); }}><option value="new">Add and save a new credential…</option>{compatible.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.username}</option>)}</select></label>
            {newCredential ? <><label>Credential name<input value={credentialName} onChange={(event) => { setCredentialName(event.target.value); setReviewed(false); }} /></label><label>Username<input autoComplete="username" value={username} onChange={(event) => { setUsername(event.target.value); setReviewed(false); }} /></label><label>Password<input type="password" autoComplete="new-password" value={secret} onChange={(event) => { setSecret(event.target.value); setReviewed(false); }} /></label><label>Availability<select value={credentialScope} onChange={(event) => setCredentialScope(event.target.value as "global" | "endpoint")}><option value="endpoint">Current endpoint</option><option value="global">All endpoints</option></select></label></> : null}
            {families.has("windows") ? <><label>WinRM transport<select value={windowsTransport} onChange={(event) => { const transport = event.target.value as "http" | "https"; setWindowsTransport(transport); setWindowsPort(transport === "https" ? 5986 : 5985); setReviewed(false); }}><option value="http">HTTP with encrypted NTLM messages</option><option value="https">HTTPS</option></select></label><label>WinRM port<input type="number" min={1} max={65535} value={windowsPort} onChange={(event) => { setWindowsPort(Number(event.target.value)); setReviewed(false); }} /></label>{windowsTransport === "https" ? <label className="check"><input type="checkbox" checked={validateCertificate} onChange={(event) => setValidateCertificate(event.target.checked)} /> Validate TLS certificate</label> : null}</> : null}
            {families.has("linux") ? <><label>SSH port<input type="number" min={1} max={65535} value={linuxPort} onChange={(event) => { setLinuxPort(Number(event.target.value)); setReviewed(false); }} /></label><label className="check"><input type="checkbox" checked={sudo} onChange={(event) => setSudo(event.target.checked)} /> Use sudo for package installation</label></> : null}
          </div>
          <div className="tools-targets">
            {targets.map((target, index) => <article key={target.vm_id} className="tools-target"><strong>{target.name}</strong><label>Guest address<input value={target.address} onChange={(event) => updateTarget(index, { address: event.target.value })} /></label><label>Operating system<select value={target.os_family} onChange={(event) => updateTarget(index, { os_family: event.target.value as "windows" | "linux" })}><option value="windows">Windows</option><option value="linux">Linux</option></select></label>{target.ssh_host_key_sha256 ? <small className="host-key">SSH host key: {target.ssh_host_key_sha256}</small> : null}{target.error ? <small className="bad-text">{target.error}</small> : null}</article>)}
          </div>
          {reviewed ? <div className="banner">Ready. Linux host keys shown above will be pinned to these jobs; a changed key stops execution.</div> : null}
          <div className="modal-actions"><button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>{reviewed ? <button className="accent" disabled={busy} onClick={() => setConfirming(true)}>Queue {targets.length} deployment{targets.length === 1 ? "" : "s"}</button> : <button className="accent" disabled={busy} onClick={() => void review()}>{busy ? "Checking…" : "Review deployment"}</button>}</div>
        </div>
      </div>

      {confirming ? <div className="modal-back tools-confirm-back" onClick={() => !busy && setConfirming(false)}><div className="modal confirm-modal" role="alertdialog" onClick={(event) => event.stopPropagation()}><h2>Confirm Tools deployment</h2><p>Queue {targets.length} independent jobs? Windows guests may reboot. Linux guests will install or update <code>open-vm-tools</code> and start its service.</p><ul>{targets.map((target) => <li key={target.vm_id}>{target.name} · {target.os_family}</li>)}</ul><div className="modal-actions"><button className="ghost" disabled={busy} onClick={() => setConfirming(false)}>Back</button><button className="accent" disabled={busy} onClick={() => void queue()}>{busy ? "Queueing…" : "Confirm & queue"}</button></div></div></div> : null}
    </>
  );
}

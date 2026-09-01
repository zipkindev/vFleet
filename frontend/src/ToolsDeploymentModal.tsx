import { useEffect, useMemo, useState } from "react";
import { deployGuestTools, fetchAutomationCredentials, fetchSshHostKey, preflightGuestTools, saveAutomationCredential, testSshConnection } from "./api";
import type { AutomationCredential, ConnectionProfile, Job, JumpHostType, ToolsDeploymentTarget, VirtualMachine } from "./types";

type Props = {
  vms: VirtualMachine[];
  accessProfile?: ConnectionProfile | null;
  onClose: () => void;
  onQueued: (jobs: Job[], failures: string[], vmIds: string[]) => void;
};

type TargetRow = ToolsDeploymentTarget & { name: string; error: string };
type GuestFamily = "windows" | "linux" | "pfsense";

function guestFamily(guestOs: string): "windows" | "linux" {
  return /win/i.test(guestOs) ? "windows" : "linux";
}

export function ToolsDeploymentModal({ vms, accessProfile, onClose, onQueued }: Props) {
  const [targets, setTargets] = useState<TargetRow[]>(() => vms.map((vm) => ({
    vm_id: vm.id,
    name: vm.name,
    address: vm.ip_address || "",
    os_family: /pfsense/i.test(`${vm.name} ${vm.guest_os}`) || (/freebsd/i.test(vm.guest_os) && /router|firewall/i.test(vm.name))
      ? "pfsense"
      : guestFamily(vm.guest_os),
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
  const [useJump, setUseJump] = useState(Boolean(accessProfile?.jump_enabled));
  const [jumpAddress, setJumpAddress] = useState(accessProfile?.jump_address || "");
  const [jumpPort, setJumpPort] = useState(accessProfile?.jump_port || 22);
  const [jumpHostType, setJumpHostType] = useState<JumpHostType>(accessProfile?.jump_host_type || "auto");
  const [detectedJumpHostType, setDetectedJumpHostType] = useState<JumpHostType>("auto");
  const [jumpCredentialId, setJumpCredentialId] = useState(accessProfile?.jump_credential_id || "");
  const [jumpHostKey, setJumpHostKey] = useState(accessProfile?.jump_host_key_sha256 || "");
  const [jumpError, setJumpError] = useState("");
  const [runDryRun, setRunDryRun] = useState(true);
  const [reviewed, setReviewed] = useState(false);
  const [dryRunPassed, setDryRunPassed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const families = useMemo(() => new Set(targets.map((target) => target.os_family)), [targets]);
  const hasWindows = families.has("windows");
  const hasSshTargets = families.has("linux") || families.has("pfsense");
  const pfsenseOnly = families.size === 1 && families.has("pfsense");
  const activeJump = useJump && hasSshTargets;
  const requiredKind = hasWindows && hasSshTargets ? "service" : hasWindows ? "windows" : "ssh";
  const compatible = credentials.filter((item) =>
    (item.kind === requiredKind || item.kind === "service") && (!pfsenseOnly || item.username.trim().toLowerCase() === "root")
  );
  const jumpCredentials = credentials.filter((item) => item.kind === "ssh" || item.kind === "service");

  useEffect(() => {
    void fetchAutomationCredentials().then((result) => {
      setCredentials(result.credentials);
      const guestCandidates = result.credentials.filter((item) =>
        (item.kind === requiredKind || item.kind === "service") && (!pfsenseOnly || item.username.trim().toLowerCase() === "root")
      );
      const matching = guestCandidates.find((item) => item.id !== accessProfile?.jump_credential_id) || guestCandidates[0];
      if (matching) setCredentialId(matching.id);
      const matchingJump = result.credentials.find((item) => item.id === accessProfile?.jump_credential_id)
        || result.credentials.find((item) => item.kind === "ssh" || item.kind === "service");
      setJumpCredentialId((current) => current || matchingJump?.id || "");
      setNewCredential(!matching);
    }).catch((err) => setError(err instanceof Error ? err.message : "Could not load Automation Vault credentials"));
  }, [accessProfile?.jump_credential_id, pfsenseOnly, requiredKind]);

  function updateTarget(index: number, update: Partial<TargetRow>) {
    setTargets((current) => current.map((target, position) => position === index ? { ...target, ...update, ssh_host_key_sha256: update.address !== undefined || update.os_family !== undefined ? "" : target.ssh_host_key_sha256 } : target));
    setReviewed(false);
  }

  async function review() {
    setBusy(true);
    setError("");
    setDryRunPassed(false);
    setJumpError("");
    setJumpHostKey("");
    setDetectedJumpHostType("auto");
    const next = targets.map((target) => ({ ...target, error: "" }));
    let reviewedJumpHostKey = "";
    let reviewedJumpError = "";
    if (activeJump) {
      if (!jumpAddress.trim() || !jumpCredentialId) {
        reviewedJumpError = "Enter a jump-host address and select its stored SSH credential";
      } else {
        try {
          reviewedJumpHostKey = await fetchSshHostKey(jumpAddress.trim(), jumpPort);
          const jumpTest = await testSshConnection({
            address: jumpAddress.trim(),
            port: jumpPort,
            credential_id: jumpCredentialId,
            host_key_sha256: reviewedJumpHostKey,
            host_type: jumpHostType,
          });
          setDetectedJumpHostType(jumpTest.detected_host_type);
        } catch (err) {
          reviewedJumpError = err instanceof Error ? err.message : "Could not read the SSH jump-host key";
        }
      }
    }
    setJumpHostKey(reviewedJumpHostKey);
    setJumpError(reviewedJumpError);
    for (const target of next) {
      if (!target.address.trim()) {
        target.error = "Enter an IP address or DNS name";
        continue;
      }
      if (target.os_family === "linux" || target.os_family === "pfsense") {
        if (reviewedJumpError) {
          target.error = "Resolve the SSH jump-host error first";
          continue;
        }
        try {
          target.ssh_host_key_sha256 = await fetchSshHostKey(
            target.address.trim(),
            linuxPort,
            activeJump ? {
              address: jumpAddress.trim(),
              port: jumpPort,
              credential_id: jumpCredentialId,
              host_key_sha256: reviewedJumpHostKey,
            } : undefined,
          );
        } catch (err) {
          target.error = err instanceof Error ? err.message : "Could not read the SSH host key";
        }
      }
    }
    const invalidCredential = newCredential
      ? !credentialName.trim() || !username.trim() || !secret
      : !credentialId;
    if (invalidCredential) setError(newCredential ? "Name, username, and password are required for the new credential" : "Select a stored credential");
    let remotePreflightPassed = !runDryRun || newCredential;
    if (runDryRun && !invalidCredential && !newCredential && !reviewedJumpError && next.every((target) => !target.error)) {
      remotePreflightPassed = true;
      for (const target of next) {
        try {
          await preflightGuestTools({
            targets: [{
              vm_id: target.vm_id,
              address: target.address.trim(),
              os_family: target.os_family,
              ssh_host_key_sha256: target.ssh_host_key_sha256,
            }],
            credential_id: credentialId,
            windows_transport: windowsTransport,
            windows_port: windowsPort,
            validate_certificate: validateCertificate,
            linux_port: linuxPort,
            sudo,
            jump_address: activeJump ? jumpAddress.trim() : "",
            jump_port: jumpPort,
            jump_host_type: jumpHostType,
            jump_credential_id: activeJump ? jumpCredentialId : "",
            jump_host_key_sha256: activeJump ? reviewedJumpHostKey : "",
          });
        } catch (err) {
          target.error = err instanceof Error ? err.message : "Guest preflight failed";
          remotePreflightPassed = false;
        }
      }
    }
    setTargets(next);
    setDryRunPassed(runDryRun && remotePreflightPassed && !newCredential);
    setReviewed(!invalidCredential && !reviewedJumpError && remotePreflightPassed && next.every((target) => !target.error));
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
        jump_address: activeJump ? jumpAddress.trim() : "",
        jump_port: jumpPort,
        jump_host_type: jumpHostType,
        jump_credential_id: activeJump ? jumpCredentialId : "",
        jump_host_key_sha256: activeJump ? jumpHostKey : "",
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
          <p>Windows uses WinRM and the ESXi-hosted Tools installer. Linux uses its distribution’s <code>open-vm-tools</code> package. pfSense uses the signed <code>pfSense-pkg-Open-VM-Tools</code> package as root. SSH targets and optional jump hosts use pinned host keys. Every VM becomes an independent persistent job.</p>
          {error ? <div className="banner bad">{error}</div> : null}
          <div className="tools-settings-grid">
            <label>Guest credential<select value={newCredential ? "new" : credentialId} onChange={(event) => { setNewCredential(event.target.value === "new"); setCredentialId(event.target.value === "new" ? "" : event.target.value); setReviewed(false); }}><option value="new">Add and save a new guest credential…</option>{compatible.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.username}</option>)}</select>{pfsenseOnly ? <small>pfSense guest access lists root credentials only.</small> : null}</label>
            {newCredential ? <><label>Credential name<input value={credentialName} onChange={(event) => { setCredentialName(event.target.value); setReviewed(false); }} /></label><label>Username<input autoComplete="username" value={username} onChange={(event) => { setUsername(event.target.value); setReviewed(false); }} /></label><label>Password<input type="password" autoComplete="new-password" value={secret} onChange={(event) => { setSecret(event.target.value); setReviewed(false); }} /></label><label>Availability<select value={credentialScope} onChange={(event) => setCredentialScope(event.target.value as "global" | "endpoint")}><option value="endpoint">Current endpoint</option><option value="global">All endpoints</option></select></label></> : null}
            {families.has("windows") ? <><label>WinRM transport<select value={windowsTransport} onChange={(event) => { const transport = event.target.value as "http" | "https"; setWindowsTransport(transport); setWindowsPort(transport === "https" ? 5986 : 5985); setReviewed(false); }}><option value="http">HTTP with encrypted NTLM messages</option><option value="https">HTTPS</option></select></label><label>WinRM port<input type="number" min={1} max={65535} value={windowsPort} onChange={(event) => { setWindowsPort(Number(event.target.value)); setReviewed(false); }} /></label>{windowsTransport === "https" ? <label className="check"><input type="checkbox" checked={validateCertificate} onChange={(event) => setValidateCertificate(event.target.checked)} /> Validate TLS certificate</label> : null}</> : null}
            {hasSshTargets ? <><label>SSH port<input type="number" min={1} max={65535} value={linuxPort} onChange={(event) => { setLinuxPort(Number(event.target.value)); setReviewed(false); }} /></label>{families.has("linux") ? <label className="check"><input type="checkbox" checked={sudo} onChange={(event) => setSudo(event.target.checked)} /> Use sudo for Linux package installation</label> : null}<label className="check"><input type="checkbox" checked={useJump} onChange={(event) => { setUseJump(event.target.checked); setJumpError(""); setReviewed(false); }} /> Connect through an SSH jump host</label>{accessProfile?.jump_enabled ? <small>Inherited from connection “{accessProfile.name}”. Disable or edit it for this deployment without changing the saved profile.</small> : null}</> : null}
            {hasSshTargets && useJump ? <><label>Jump-host address<input value={jumpAddress} onChange={(event) => { setJumpAddress(event.target.value); setJumpHostKey(""); setReviewed(false); }} /></label><label>Jump-host SSH port<input type="number" min={1} max={65535} value={jumpPort} onChange={(event) => { setJumpPort(Number(event.target.value)); setJumpHostKey(""); setReviewed(false); }} /></label><label>Jump-host credential<select value={jumpCredentialId} onChange={(event) => { setJumpCredentialId(event.target.value); setJumpHostKey(""); setReviewed(false); }}><option value="">Select stored SSH credential…</option>{jumpCredentials.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.username}</option>)}</select></label><label>Jump-host type<select value={jumpHostType} onChange={(event) => { setJumpHostType(event.target.value as JumpHostType); setJumpHostKey(""); setDetectedJumpHostType("auto"); setReviewed(false); }}><option value="auto">Auto detect</option><option value="windows">Windows OpenSSH (PowerShell)</option><option value="unix">Linux or Unix</option></select></label>{jumpHostKey ? <small className="host-key">Jump-host key: {jumpHostKey}{jumpHostType === "auto" ? ` · ${detectedJumpHostType === "auto" ? "type undetermined" : `detected ${detectedJumpHostType}`}` : ""}</small> : null}{jumpError ? <small className="bad-text">{jumpError}</small> : null}</> : null}
            <label className="check"><input type="checkbox" checked={runDryRun} onChange={(event) => { setRunDryRun(event.target.checked); setReviewed(false); setDryRunPassed(false); }} /> Run a remote dry run before queueing</label>
            <small>The dry run verifies credentials, platform access, reachability, and pinned SSH keys without installing VMware Tools.</small>
          </div>
          <div className="tools-targets">
            {targets.map((target, index) => <article key={target.vm_id} className="tools-target"><strong>{target.name}</strong><label>Guest address<input value={target.address} onChange={(event) => updateTarget(index, { address: event.target.value })} /></label><label>Operating system<select value={target.os_family} onChange={(event) => updateTarget(index, { os_family: event.target.value as GuestFamily })}><option value="windows">Windows</option><option value="linux">Linux</option><option value="pfsense">pfSense</option></select></label>{target.os_family === "pfsense" ? <small>Requires a root SSH credential. No reboot is requested.</small> : null}{target.ssh_host_key_sha256 ? <small className="host-key">SSH host key: {target.ssh_host_key_sha256}</small> : null}{target.error ? <small className="bad-text">{target.error}</small> : null}</article>)}
          </div>
          {reviewed ? <div className="banner">{dryRunPassed ? "Dry run passed: credentials, platform preflight, reachability, and pinned host keys were verified without installing anything." : runDryRun && newCredential ? "Ready. The new credential will be saved when queued; remote dry run requires a previously saved credential. Pinned SSH keys will still be enforced." : "Review passed with the remote dry run skipped. Every SSH host key shown above will be pinned to these jobs; a changed target or jump-host key stops execution."}</div> : null}
          <div className="modal-actions"><button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>{reviewed ? <button className="accent" disabled={busy} onClick={() => setConfirming(true)}>Queue {targets.length} deployment{targets.length === 1 ? "" : "s"}</button> : <button className="accent" disabled={busy} onClick={() => void review()}>{busy ? "Checking…" : "Review deployment"}</button>}</div>
        </div>
      </div>

      {confirming ? <div className="modal-back tools-confirm-back" onClick={() => !busy && setConfirming(false)}><div className="modal confirm-modal" role="alertdialog" onClick={(event) => event.stopPropagation()}><h2>Confirm Tools deployment</h2><p>Queue {targets.length} independent jobs? Windows guests may reboot. Linux guests install <code>open-vm-tools</code>. pfSense guests install the supported <code>pfSense-pkg-Open-VM-Tools</code> package and start its services without requesting a reboot.</p><ul>{targets.map((target) => <li key={target.vm_id}>{target.name} · {target.os_family}</li>)}</ul><div className="modal-actions"><button className="ghost" disabled={busy} onClick={() => setConfirming(false)}>Back</button><button className="accent" disabled={busy} onClick={() => void queue()}>{busy ? "Queueing…" : "Confirm & queue"}</button></div></div></div> : null}
    </>
  );
}

import { useEffect, useState } from "react";
import { HostUpgradePanel } from "./HostUpgradePanel";
import {
  fetchHostManagement,
  queueHostAction,
  queueHostService,
  queueHostTime,
  queueStorageRescan,
  queueSupportBundle,
} from "./api";
import type { HostManagementInfo, Job } from "./types";

export function HostManagementView({ onQueued }: { onQueued: (job: Job) => void }) {
  const [host, setHost] = useState<HostManagementInfo | null>(null);
  const [ntp, setNtp] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try {
      const next = await fetchHostManagement();
      setHost(next);
      setNtp(next.ntp_servers.join(", "));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load host administration");
    }
  }

  useEffect(() => { void load(); }, []);

  async function run(key: string, work: () => Promise<Job>, question: string) {
    if (!window.confirm(question)) return;
    setBusy(key);
    setError("");
    try {
      onQueued(await work());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue host operation");
    } finally {
      setBusy("");
    }
  }

  if (error && !host) return <div className="panel"><div className="banner bad">{error}</div><button onClick={() => void load()}>Retry</button></div>;
  if (!host) return <div className="panel empty">Loading direct ESXi administration…</div>;

  return (
    <section className="stack host-admin">
      {error ? <div className="banner bad">{error}</div> : null}
      <div className="panel">
        <header>
          <div><h2>{host.name}</h2><p>{host.product_name} · build {host.build}</p></div>
          <span className={`chip ${host.maintenance_mode ? "warm" : "ok"}`}>{host.maintenance_mode ? "Maintenance" : host.connection_state}</span>
        </header>
        <div className="host-facts">
          <div><span>Hardware</span><strong>{host.vendor} {host.model}</strong></div>
          <div><span>Uptime</span><strong>{Math.floor(host.uptime_seconds / 86400)} days</strong></div>
          <div><span>API</span><strong>{host.api_version}</strong></div>
          <div><span>SSH fallback</span><strong>{host.ssh_configured ? "Configured" : "Off"}</strong></div>
        </div>
      </div>

      <HostUpgradePanel />

      <div className="split">
        <div className="panel">
          <header><div><h2>Safe host lifecycle</h2><p>Host-wide changes run through the persistent job relay.</p></div></header>
          <div className="button-grid">
            <button disabled={Boolean(busy) || host.maintenance_mode} onClick={() => void run("enter", () => queueHostAction("maintenance_enter"), "Enter maintenance mode? All VMs must be powered off.")}>Enter maintenance</button>
            <button disabled={Boolean(busy) || !host.maintenance_mode} onClick={() => void run("exit", () => queueHostAction("maintenance_exit"), "Exit maintenance mode?")}>Exit maintenance</button>
            <button className="danger" disabled={Boolean(busy) || !host.maintenance_mode} onClick={() => void run("reboot", () => queueHostAction("reboot"), "Reboot this ESXi host? It must be in maintenance mode with no powered-on VMs.")}>Reboot host</button>
            <button className="danger" disabled={Boolean(busy) || !host.maintenance_mode} onClick={() => void run("shutdown", () => queueHostAction("shutdown"), "Shut down this ESXi host? It will require physical or out-of-band access to start again.")}>Shut down host</button>
            <button disabled={Boolean(busy)} onClick={() => void run("rescan", queueStorageRescan, "Rescan all host storage adapters and VMFS volumes?")}>Rescan storage</button>
            <button disabled={Boolean(busy)} onClick={() => void run("bundle", queueSupportBundle, "Generate an ESXi diagnostic support bundle?")}>Support bundle</button>
          </div>
        </div>
        <div className="panel">
          <header><div><h2>Time and NTP</h2><p>Comma-separated hostnames or IP addresses.</p></div></header>
          <label>NTP servers<input value={ntp} onChange={(event) => setNtp(event.target.value)} placeholder="pool.ntp.org" /></label>
          <button className="accent" disabled={Boolean(busy)} onClick={() => void run("ntp", () => queueHostTime(ntp.split(",").map((item) => item.trim()).filter(Boolean), true), "Save these NTP servers and restart ntpd?")}>Save & sync</button>
          <p className="sub">Host time: {host.current_time ? new Date(host.current_time).toLocaleString() : "Unavailable"}</p>
        </div>
      </div>

      <div className="panel">
        <header><div><h2>Services</h2><p>Only ESXi Shell, SSH, and NTP are exposed for control.</p></div></header>
        <table><thead><tr><th>Service</th><th>State</th><th>Policy</th><th>Actions</th></tr></thead><tbody>
          {host.services.map((service) => (
            <tr key={service.key}>
              <td>{service.label}<small className="sub">{service.key}</small></td>
              <td><span className={`chip ${service.running ? "ok" : "muted"}`}>{service.running ? "Running" : "Stopped"}</span></td>
              <td>{service.policy || "—"}</td>
              <td>{service.controllable ? <div className="inline-actions">
                <button disabled={Boolean(busy) || service.running} onClick={() => void run(service.key, () => queueHostService(service.key, "start"), `Start ${service.label}?`)}>Start</button>
                <button disabled={Boolean(busy) || !service.running || service.required} onClick={() => void run(service.key, () => queueHostService(service.key, "stop"), `Stop ${service.label}?`) }>Stop</button>
                <button disabled={Boolean(busy) || !service.running} onClick={() => void run(service.key, () => queueHostService(service.key, "restart"), `Restart ${service.label}?`)}>Restart</button>
              </div> : <span className="sub">Read only</span>}</td>
            </tr>
          ))}
        </tbody></table>
      </div>
    </section>
  );
}

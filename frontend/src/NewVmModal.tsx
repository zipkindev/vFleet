import { FormEvent, useMemo, useState } from "react";
import { deployFromTemplate } from "./api";
import { FolderPicker } from "./FolderPicker";
import type { Catalog, ClusterSummary, HostSummary, Job } from "./types";

type Props = {
  catalog: Catalog | null;
  clusters: ClusterSummary[];
  hosts: HostSummary[];
  initialTemplateId?: string;
  onClose: () => void;
  onQueued: (job: Job) => void;
};

export function NewVmModal({
  catalog,
  clusters,
  hosts,
  initialTemplateId,
  onClose,
  onQueued,
}: Props) {
  const templates = catalog?.templates ?? [];
  const allDatastores = catalog?.datastores ?? [];
  const bootTemplate = templates.find((item) => item.id === (initialTemplateId || templates[0]?.id || ""));
  const [templateId, setTemplateId] = useState(initialTemplateId || templates[0]?.id || "");
  const selected = useMemo(() => templates.find((item) => item.id === templateId), [templates, templateId]);
  const [name, setName] = useState("");
  const [datastoreId, setDatastoreId] = useState(bootTemplate?.datastore_id || allDatastores[0]?.id || "");
  const [clusterId, setClusterId] = useState(bootTemplate?.cluster_id || clusters[0]?.id || "");
  const [hostId, setHostId] = useState("");
  const [folderId, setFolderId] = useState("");
  const [disableDrs, setDisableDrs] = useState(false);
  const [cpu, setCpu] = useState(String(bootTemplate?.cpu_count || 4));
  const [memoryGib, setMemoryGib] = useState(String((bootTemplate?.memory_mib || 8192) / 1024));
  const [powerOn, setPowerOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const clusterHosts = useMemo(() => {
    if (!clusterId) {
      return hosts.filter((host) => host.connection_state.toLowerCase() !== "disconnected");
    }
    return hosts.filter(
      (host) => host.cluster_id === clusterId && host.connection_state.toLowerCase() !== "disconnected",
    );
  }, [hosts, clusterId]);

  const datastores = useMemo(() => {
    if (!hostId) return allDatastores;
    return allDatastores.filter((item) => !item.host_ids.length || item.host_ids.includes(hostId));
  }, [allDatastores, hostId]);

  function applyTemplate(id: string) {
    setTemplateId(id);
    const next = templates.find((item) => item.id === id);
    if (!next) return;
    if (next.datastore_id) setDatastoreId(next.datastore_id);
    if (next.cluster_id) setClusterId(next.cluster_id);
    setHostId("");
    setDisableDrs(false);
    setCpu(String(next.cpu_count || 4));
    setMemoryGib(String((next.memory_mib || 8192) / 1024));
  }

  function onClusterChange(nextClusterId: string) {
    setClusterId(nextClusterId);
    setHostId("");
    setDisableDrs(false);
  }

  function onHostChange(nextHostId: string) {
    setHostId(nextHostId);
    if (!nextHostId) setDisableDrs(false);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!templateId || !name.trim() || !datastoreId) {
      setError("Name, template, and datastore are required");
      return;
    }
    if (disableDrs && !hostId) {
      setError("Pick a destination host to use a DRS override");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await deployFromTemplate({
        template_id: templateId,
        name: name.trim(),
        datastore_id: datastoreId,
        cluster_id: clusterId,
        host_id: hostId || undefined,
        folder_id: folderId || undefined,
        disable_drs: disableDrs,
        cpu_count: Number(cpu) || undefined,
        memory_mib: Math.round(Number(memoryGib) * 1024) || undefined,
        power_on: powerOn,
      });
      onQueued(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to queue VM");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" onClick={onClose}>
      <form className="modal login-modal" onClick={(event) => event.stopPropagation()} onSubmit={onSubmit}>
        <h2>New virtual machine</h2>
        <p>
          Provisions a VM on the remote side. The request is queued locally and resumes if vCenter or the VPN drops.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {templates.length === 0 ? (
          <p className="empty">No templates in the local catalog yet. Connect once so vFleet can cache them.</p>
        ) : (
          <fieldset className="choice">
            <legend>Deploy from template</legend>
            <p className="migrate-summary">
              Clones a template already on vCenter — a small API call, so it survives a flaky VPN much better than
              uploading a disk.
            </p>
            <label>
              Template
              <select value={templateId} onChange={(event) => applyTemplate(event.target.value)}>
                {templates.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                    {item.guest_os ? ` · ${item.guest_os}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Name
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="atlasdemo-win11-lab2" required />
            </label>
            <div className="login-grid">
              <label>
                Cluster
                <select value={clusterId} onChange={(event) => onClusterChange(event.target.value)}>
                  <option value="">Template default</option>
                  {clusters.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                vCPU
                <input value={cpu} onChange={(event) => setCpu(event.target.value)} inputMode="numeric" />
              </label>
            </div>
            <label>
              Destination host
              <select value={hostId} onChange={(event) => onHostChange(event.target.value)}>
                <option value="">DRS picks host (default)</option>
                {clusterHosts.map((host) => (
                  <option key={host.id} value={host.id}>
                    {host.name}
                    {` · ${host.cpu_usage_pct.toFixed(0)}% CPU · ${host.memory_usage_pct.toFixed(0)}% RAM`}
                  </option>
                ))}
              </select>
            </label>
            <FolderPicker value={folderId} onChange={setFolderId} defaultLabel="Same as template (default)" />
            {hostId ? (
              <fieldset className="choice">
                <label className="check">
                  <input type="checkbox" checked={disableDrs} onChange={(event) => setDisableDrs(event.target.checked)} />
                  Pin to the selected host (DRS override)
                </label>
                <p className="migrate-summary">
                  Adds a per-VM DRS override so vCenter will not auto-migrate this VM after deploy. Requires cluster
                  reconfigure permission.
                </p>
              </fieldset>
            ) : null}
            <div className="login-grid">
              <label>
                Datastore
                <select value={datastoreId} onChange={(event) => setDatastoreId(event.target.value)}>
                  {datastores.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Memory (GiB)
                <input value={memoryGib} onChange={(event) => setMemoryGib(event.target.value)} inputMode="decimal" />
              </label>
            </div>
            <label className="check">
              <input type="checkbox" checked={powerOn} onChange={(event) => setPowerOn(event.target.checked)} />
              Power on after deploy
            </label>
            {selected ? (
              <p className="empty">
                Source: {selected.cluster_name || "cluster"} · {selected.datastore_name || "datastore"}
              </p>
            ) : null}
          </fieldset>
        )}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="accent" disabled={busy || templates.length === 0}>
            {busy ? "Queueing…" : "Queue VM"}
          </button>
        </div>
      </form>
    </div>
  );
}

import { FormEvent, useMemo, useState } from "react";
import { cloneVm } from "./api";
import type { Catalog, ClusterSummary, Job } from "./types";

type Props = {
  catalog: Catalog | null;
  clusters: ClusterSummary[];
  onClose: () => void;
  onQueued: (job: Job) => void;
};

export function CloneVmModal({ catalog, clusters, onClose, onQueued }: Props) {
  const templates = catalog?.templates ?? [];
  const datastores = catalog?.datastores ?? [];
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const selected = useMemo(() => templates.find((item) => item.id === templateId), [templates, templateId]);
  const [name, setName] = useState("");
  const [datastoreId, setDatastoreId] = useState(templates[0]?.datastore_id || datastores[0]?.id || "");
  const [clusterId, setClusterId] = useState(templates[0]?.cluster_id || clusters[0]?.id || "");
  const [cpu, setCpu] = useState(String(templates[0]?.cpu_count || 4));
  const [memoryGib, setMemoryGib] = useState(String((templates[0]?.memory_mib || 8192) / 1024));
  const [powerOn, setPowerOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function applyTemplate(id: string) {
    setTemplateId(id);
    const next = templates.find((item) => item.id === id);
    if (!next) return;
    if (next.datastore_id) setDatastoreId(next.datastore_id);
    if (next.cluster_id) setClusterId(next.cluster_id);
    setCpu(String(next.cpu_count || 4));
    setMemoryGib(String((next.memory_mib || 8192) / 1024));
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!templateId || !name.trim() || !datastoreId) {
      setError("Name, template, and datastore are required");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await cloneVm({
        template_id: templateId,
        name: name.trim(),
        datastore_id: datastoreId,
        cluster_id: clusterId,
        cpu_count: Number(cpu) || undefined,
        memory_mib: Math.round(Number(memoryGib) * 1024) || undefined,
        power_on: powerOn,
      });
      onQueued(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Clone failed to queue");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" onClick={onClose}>
      <form className="modal login-modal" onClick={(event) => event.stopPropagation()} onSubmit={onSubmit}>
        <h2>New virtual machine</h2>
        <p>
          Clones a template that already lives on the remote side. That is a small API call, so it survives a flaky VPN
          much better than uploading a disk. The request is queued locally and resumes if vCenter drops.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {templates.length === 0 ? (
          <p className="empty">No templates in the local catalog yet. Connect once so vFleet can cache them.</p>
        ) : (
          <>
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
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="mizipkin-win11-lab2" required />
            </label>
            <div className="login-grid">
              <label>
                Cluster
                <select value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
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
              Power on after clone
            </label>
            {selected ? (
              <p className="empty">
                Source: {selected.cluster_name || "cluster"} · {selected.datastore_name || "datastore"}
              </p>
            ) : null}
          </>
        )}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="accent" disabled={busy || templates.length === 0}>
            {busy ? "Queueing…" : "Queue clone"}
          </button>
        </div>
      </form>
    </div>
  );
}

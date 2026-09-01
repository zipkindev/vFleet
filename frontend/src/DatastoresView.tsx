import React, { useEffect, useState } from "react";
import { createStaging, deleteDatastoreFile, enqueueUpload, fetchDatastoreFiles, mkdir, putStagingChunk } from "./api";
import { bytes } from "./format";
import { MigrationAccessPanel } from "./MigrationAccessPanel";
import type { Catalog, ClusterSummary, DatastoreFile, DatastoreListing, Job, VmTemplate } from "./types";

type Props = {
  catalog: Catalog | null;
  clusters: ClusterSummary[];
  onQueued: (job: Job, message: string) => void;
  onNotice?: (message: string) => void;
  onNewVmFromTemplate?: (templateId: string) => void;
  onReconcile?: () => void;
};

const CHUNK = 4 * 1024 * 1024;

export const DatastoresView = React.memo(function DatastoresView({
  catalog,
  clusters,
  onQueued,
  onNotice,
  onNewVmFromTemplate,
  onReconcile,
}: Props) {
  const datastores = catalog?.datastores ?? [];
  const templates = catalog?.templates ?? [];
  const [selectedId, setSelectedId] = useState(datastores[0]?.id ?? "");
  const [listing, setListing] = useState<DatastoreListing | null>(null);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState("");
  const [useLibrary, setUseLibrary] = useState(true);

  const selected = datastores.find((item) => item.id === selectedId) ?? datastores[0];

  async function loadFiles(datastoreId: string, folderPath: string) {
    if (!datastoreId) return;
    setError("");
    try {
      const result = await fetchDatastoreFiles(datastoreId, folderPath);
      setListing(result);
      setPath(result.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not browse datastore");
    }
  }

  useEffect(() => {
    if (!selectedId && datastores[0]) setSelectedId(datastores[0].id);
  }, [datastores, selectedId]);

  useEffect(() => {
    if (selectedId) void loadFiles(selectedId, "");
  }, [selectedId]);

  async function onUpload(file: File) {
    if (!selected) return;
    setBusy("staging");
    try {
      const session = await createStaging(file.name, file.size);
      for (let start = 0; start < file.size; start += CHUNK) {
        const blob = file.slice(start, Math.min(file.size, start + CHUNK));
        await putStagingChunk(session.id, blob, start, file.size);
      }
      const remote = [path, file.name].filter(Boolean).join("/");
      const job = await enqueueUpload({
        staging_id: session.id,
        datastore_id: selected.id,
        remote_path: remote,
        use_library: useLibrary,
      });
      onQueued(job, `Queued upload of ${file.name}. The relay will resume if the VPN drops.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy("");
    }
  }

  async function onMkdir() {
    if (!selected || !folder.trim()) return;
    const job = await mkdir(selected.id, [path, folder.trim()].filter(Boolean).join("/"));
    setFolder("");
    onQueued(job, "Folder create queued");
  }

  async function onDelete(item: DatastoreFile) {
    if (!selected) return;
    if (!window.confirm(`Delete ${item.path} from ${selected.name}?`)) return;
    const job = await deleteDatastoreFile(selected.id, item.path);
    onQueued(job, `Queued delete of ${item.name}`);
  }

  const crumbs = path ? path.split("/").filter(Boolean) : [];

  return (
    <section className="stack">
      {catalog?.stale ? (
        <div className="banner">Showing last cached datastores. Browse/upload will queue or retry when the vSphere endpoint is reachable.</div>
      ) : null}
      {error ? <div className="banner bad">{error}</div> : null}
      <MigrationAccessPanel clusters={clusters} onDone={(message) => onNotice?.(message)} />
      {templates.length > 0 ? (
        <div className="panel">
          <header>
            <div>
              <h2>Templates</h2>
              <p>Deploy a VM from a vCenter template. Pick a host to pin placement, or optionally add a DRS override.</p>
            </div>
          </header>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Cluster</th>
                <th>Datastore</th>
                <th>Size</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {templates.map((item: VmTemplate) => (
                <tr key={item.id}>
                  <td>
                    {item.name}
                    {item.guest_os ? <small className="sub">{item.guest_os}</small> : null}
                  </td>
                  <td>{item.cluster_name || "—"}</td>
                  <td>{item.datastore_name || "—"}</td>
                  <td>
                    {item.cpu_count ? `${item.cpu_count} vCPU` : "—"}
                    {item.memory_mib ? ` · ${Math.round(item.memory_mib / 1024)} GiB` : ""}
                  </td>
                  <td>
                    <button className="text" onClick={() => onNewVmFromTemplate?.(item.id)}>
                      New VM…
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <div className="panel">
        <header>
          <div>
            <h2>Datastores</h2>
            <p>Run a guarded reconciliation to find preserved conversion sources, unattached disks, and unregistered VM folders.</p>
          </div>
          <button className="ghost compact" onClick={onReconcile}>Reconcile inventory</button>
        </header>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Type</th>
              <th>Capacity</th>
              <th>Free</th>
              <th>Used</th>
            </tr>
          </thead>
          <tbody>
            {datastores.map((item) => (
              <tr
                key={item.id}
                className={item.id === selected?.id ? "idle" : ""}
                onClick={() => setSelectedId(item.id)}
                style={{ cursor: "pointer" }}
              >
                <td>
                  {item.name}
                  <small className="sub">{item.datacenter || item.host_count + " hosts"}</small>
                </td>
                <td>
                  {item.type || "—"}
                  {item.readonly ? <small className="sub">read-only</small> : null}
                </td>
                <td>{bytes(item.capacity_bytes)}</td>
                <td>{bytes(item.free_bytes)}</td>
                <td>
                  <Meter value={item.usage_pct} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected ? (
        <div className="panel">
          <header>
            <div>
              <h2>{selected.name}</h2>
              <p>
                {selected.readonly ? "Mounted read-only on ESXi — uploads are rejected. " : ""}
                <button className="text" onClick={() => void loadFiles(selected.id, "")}>
                  root
                </button>
                {crumbs.map((part, index) => {
                  const next = crumbs.slice(0, index + 1).join("/");
                  return (
                    <span key={next}>
                      {" / "}
                      <button className="text" onClick={() => void loadFiles(selected.id, next)}>
                        {part}
                      </button>
                    </span>
                  );
                })}
                {listing?.stale ? " · cached listing" : ""}
              </p>
            </div>
            <div className="header-meta">
              <label className="check tight">
                <input type="checkbox" checked={useLibrary} onChange={(event) => setUseLibrary(event.target.checked)} />
                Prefer content library (resumable)
              </label>
              <label className="accent file-btn">
                {busy === "staging" ? "Staging locally…" : selected.readonly ? "Read-only datastore" : "Upload file"}
                <input
                  type="file"
                  hidden
                  disabled={Boolean(busy) || Boolean(selected.readonly)}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file) void onUpload(file);
                  }}
                />
              </label>
            </div>
          </header>
          <div className="folder-row">
            <input value={folder} onChange={(event) => setFolder(event.target.value)} placeholder="new-folder" />
            <button className="ghost" onClick={() => void onMkdir()} disabled={!folder.trim()}>
              Create folder
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Size</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(listing?.files ?? []).map((item) => (
                <tr key={item.path}>
                  <td>
                    {item.is_directory ? (
                      <button className="text" onClick={() => void loadFiles(selected.id, item.path)}>
                        {item.name}/
                      </button>
                    ) : (
                      item.name
                    )}
                  </td>
                  <td>{item.kind}</td>
                  <td>{item.is_directory ? "—" : bytes(item.size)}</td>
                  <td>
                    <button className="text" onClick={() => void onDelete(item)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(listing?.files ?? []).length === 0 ? <p className="empty pad">Empty folder, or waiting on vCenter.</p> : null}
        </div>
      ) : null}
    </section>
  );
});

function Meter({ value }: { value: number }) {
  const tone = value >= 85 ? "hot" : value >= 60 ? "warm" : "ok";
  return (
    <div className="meter">
      <div className={tone} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      <em>{value.toFixed(0)}%</em>
    </div>
  );
}

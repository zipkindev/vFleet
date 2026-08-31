import { FormEvent, useMemo, useState } from "react";
import { cloneMigrate, migrateVms } from "./api";
import { FolderPicker } from "./FolderPicker";
import { diskLabel } from "./format";
import type { Catalog, HostSummary, Job, VirtualMachine } from "./types";

type Mode = "clone" | "clone_destroy" | "relocate";

type Props = {
  vms: VirtualMachine[];
  hosts: HostSummary[];
  catalog: Catalog | null;
  onClose: () => void;
  onQueued: (job: Job) => void;
};

export function MigrateVmModal({ vms, hosts, catalog, onClose, onQueued }: Props) {
  const source = vms[0];
  const clusterIds = useMemo(() => [...new Set(vms.map((vm) => vm.cluster_id).filter(Boolean))], [vms]);
  const clusterName = source?.cluster_name || "this cluster";
  const sameCluster = clusterIds.length <= 1;
  const clusterHosts = useMemo(() => {
    const clusterId = clusterIds[0] || "";
    return hosts.filter((host) => {
      if (!sameCluster) return host.connection_state.toLowerCase() !== "disconnected";
      return host.cluster_id === clusterId && host.connection_state.toLowerCase() !== "disconnected";
    });
  }, [hosts, clusterIds, sameCluster]);

  const currentHostIds = useMemo(() => new Set(vms.map((vm) => vm.host_id)), [vms]);
  const defaultHost = clusterHosts.find((host) => !currentHostIds.has(host.id))?.id || "";
  const [mode, setMode] = useState<Mode>("clone");
  const [hostId, setHostId] = useState(defaultHost);
  const [folderId, setFolderId] = useState("");
  const [cloneName, setCloneName] = useState(source ? `${source.name}-clone` : "");
  const [disableDrs, setDisableDrs] = useState(false);
  const [destroyConfirm, setDestroyConfirm] = useState(false);
  const [changeStorage, setChangeStorage] = useState(false);
  const [datastoreId, setDatastoreId] = useState("");
  const [diskProvisioning, setDiskProvisioning] = useState("");
  const [changeNetwork, setChangeNetwork] = useState(false);
  const [networkId, setNetworkId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const isCloneMode = mode === "clone" || mode === "clone_destroy";
  const cloneNeedsSingle = isCloneMode && vms.length !== 1;

  const destHost = clusterHosts.find((host) => host.id === hostId);
  const destHostId = destHost?.id || (isCloneMode ? "" : source?.host_id || "");
  const datastores = useMemo(() => {
    const rows = catalog?.datastores ?? [];
    return rows.filter((item) => !item.host_ids.length || !destHostId || item.host_ids.includes(destHostId));
  }, [catalog, destHostId]);
  const networks = useMemo(() => {
    const rows = catalog?.networks ?? [];
    return rows.filter((item) => !item.host_ids.length || !destHostId || item.host_ids.includes(destHostId));
  }, [catalog, destHostId]);

  const diskCounts = useMemo(() => {
    const counts = { thin: 0, thick: 0, mixed: 0, unknown: 0 };
    for (const vm of vms) {
      const kind =
        vm.disk_provisioning === "thin" || vm.disk_provisioning === "thick" || vm.disk_provisioning === "mixed"
          ? vm.disk_provisioning
          : "unknown";
      counts[kind] += 1;
    }
    return counts;
  }, [vms]);

  const relocateHasChange = Boolean(hostId || changeStorage || diskProvisioning || changeNetwork || folderId);
  const crossCluster = Boolean(hostId && !sameCluster);

  const planSummary = useMemo(() => {
    const names = vms.length === 1 ? source?.name || "VM" : `${vms.length} VMs`;
    const fromHost = source?.host_name || "current host";
    const toHost = destHost?.name || (mode === "relocate" ? "current host" : "pick a host");
    const pin = disableDrs ? "DRS pin: yes" : "DRS pin: no";
    if (mode === "clone") {
      return `${names} on ${fromHost} → clone as ${cloneName.trim() || "…"} on ${toHost} · keep original · ${pin}`;
    }
    if (mode === "clone_destroy") {
      return `${names} on ${fromHost} → replace onto ${toHost} · destroy original after success · ${pin}`;
    }
    const storage = changeStorage ? `datastore ${datastores.find((d) => d.id === datastoreId)?.name || "…"}` : "keep datastore";
    const nics = changeNetwork ? `network ${networks.find((n) => n.id === networkId)?.name || "…"}` : "keep NICs";
    const disk = diskProvisioning ? `→ ${diskProvisioning}` : "no disk convert";
    return `${names} · host ${toHost} · ${storage} · ${nics} · ${disk}`;
  }, [
    vms.length,
    source,
    destHost,
    mode,
    cloneName,
    disableDrs,
    changeStorage,
    datastoreId,
    datastores,
    changeNetwork,
    networkId,
    networks,
    diskProvisioning,
  ]);

  function onModeChange(next: Mode) {
    setMode(next);
    setError("");
    setDestroyConfirm(false);
    if (next !== "clone") setCloneName(source ? `${source.name}-clone` : "");
    if (next === "relocate") setDisableDrs(false);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (cloneNeedsSingle) {
      setError("Clone modes support one VM. Reduce the selection or switch to Relocate (vMotion).");
      return;
    }
    if (isCloneMode) {
      if (!hostId) {
        setError("Pick a destination host for clone");
        return;
      }
      if (mode === "clone" && !cloneName.trim()) {
        setError("Clone name is required");
        return;
      }
      if (mode === "clone_destroy" && !destroyConfirm) {
        setError("Confirm that the original VM will be deleted after the clone succeeds");
        return;
      }
      if (crossCluster) {
        setError("Clone destination must stay in the same cluster.");
        return;
      }
    } else {
      if (!relocateHasChange) {
        setError("Pick a destination host, datastore, network, disk type, or folder");
        return;
      }
      if (crossCluster) {
        setError("Selected VMs span more than one cluster. Keep the current host, or select VMs from a single cluster.");
        return;
      }
      if (changeStorage && !datastoreId) {
        setError("Pick a destination datastore, or keep the current location");
        return;
      }
      if (changeNetwork && !networkId) {
        setError("Pick a destination network, or keep the current NICs");
        return;
      }
    }

    setBusy(true);
    setError("");
    try {
      if (isCloneMode) {
        const job = await cloneMigrate({
          vm_id: source.id,
          host_id: hostId,
          name: mode === "clone" ? cloneName.trim() : undefined,
          destroy_source: mode === "clone_destroy",
          disable_drs: disableDrs,
          datastore_id: changeStorage ? datastoreId : "",
          folder_id: folderId || undefined,
          confirm: true,
        });
        onQueued(job);
      } else {
        const job = await migrateVms({
          vm_ids: vms.map((vm) => vm.id),
          host_id: hostId,
          datastore_id: changeStorage ? datastoreId : "",
          network_id: changeNetwork ? networkId : "",
          disk_provisioning: diskProvisioning,
          folder_id: folderId || undefined,
          confirm: true,
        });
        onQueued(job);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed to queue");
    } finally {
      setBusy(false);
    }
  }

  const canSubmit = isCloneMode
    ? Boolean(hostId) && !cloneNeedsSingle && !crossCluster && (mode !== "clone_destroy" || destroyConfirm)
    : relocateHasChange && !crossCluster;

  return (
    <div className="modal-back" onClick={onClose}>
      <form className="modal login-modal migrate-modal" onClick={(event) => event.stopPropagation()} onSubmit={onSubmit}>
        <h2>Migrate / Clone</h2>
        <p>
          Choose how to place VMs in {clusterName}. Use <strong>Just Clone</strong> when vMotion/swap cannot target the
          destination host. Relocate moves the existing VM when the cluster supports it.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {cloneNeedsSingle ? (
          <div className="banner">Clone modes support one VM. Reduce the selection or switch to Relocate (vMotion).</div>
        ) : null}
        {!sameCluster ? (
          <div className="banner">
            Selection spans {clusterIds.length} clusters. Keep the current host to convert disks in place, or select VMs
            from one cluster to move them together.
          </div>
        ) : null}

        <fieldset className="choice">
          <legend>Migration type</legend>
          <label className="check">
            <input type="radio" name="mode" checked={mode === "clone"} onChange={() => onModeChange("clone")} />
            Just Clone — copy onto a host; keep the original (default)
          </label>
          <label className="check">
            <input
              type="radio"
              name="mode"
              checked={mode === "clone_destroy"}
              onChange={() => onModeChange("clone_destroy")}
            />
            Clone + Destroy old — replace onto a host; delete original after success
          </label>
          <label className="check">
            <input type="radio" name="mode" checked={mode === "relocate"} onChange={() => onModeChange("relocate")} />
            Relocate (vMotion) — move the existing VM (host / datastore / disk / network)
          </label>
        </fieldset>

        <p className="migrate-summary">{planSummary}</p>
        <ul className="migrate-vms">
          {vms.slice(0, 8).map((vm) => (
            <li key={vm.id}>
              {vm.name}
              <small>
                {vm.host_name} · {diskLabel(vm.disk_provisioning)}
              </small>
            </li>
          ))}
          {vms.length > 8 ? <li>and {vms.length - 8} more</li> : null}
        </ul>

        <label>
          Destination host
          <select value={hostId} onChange={(event) => setHostId(event.target.value)}>
            <option value="">{isCloneMode ? "Select host" : "Keep current host"}</option>
            {clusterHosts.map((host) => (
              <option key={host.id} value={host.id}>
                {host.name}
                {currentHostIds.has(host.id) ? " (current)" : ""}
                {` · ${host.cpu_usage_pct.toFixed(0)}% CPU · ${host.memory_usage_pct.toFixed(0)}% RAM`}
              </option>
            ))}
          </select>
        </label>

        <FolderPicker
          value={folderId}
          onChange={setFolderId}
          defaultLabel={isCloneMode ? "Same as source (default)" : "Keep current folder"}
        />

        {mode === "clone" ? (
          <label>
            New VM name
            <input value={cloneName} onChange={(event) => setCloneName(event.target.value)} required />
          </label>
        ) : null}

        {mode === "clone_destroy" ? (
          <fieldset className="choice">
            <label className="check">
              <input
                type="checkbox"
                checked={destroyConfirm}
                onChange={(event) => setDestroyConfirm(event.target.checked)}
              />
              I understand the original VM will be deleted after the clone succeeds. The new VM keeps the original name.
            </label>
          </fieldset>
        ) : null}

        {isCloneMode ? (
          <>
            <label className="check">
              <input type="checkbox" checked={changeStorage} onChange={(event) => setChangeStorage(event.target.checked)} />
              Use a different datastore for the clone
            </label>
            {changeStorage ? (
              <label>
                Datastore
                <select value={datastoreId} onChange={(event) => setDatastoreId(event.target.value)}>
                  <option value="">Select datastore</option>
                  {datastores.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                      {item.free_bytes ? ` · ${Math.round(item.free_bytes / 1024 ** 3)} GiB free` : ""}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {hostId ? (
              <fieldset className="choice">
                <label className="check">
                  <input type="checkbox" checked={disableDrs} onChange={(event) => setDisableDrs(event.target.checked)} />
                  Pin to the selected host (DRS override)
                </label>
                <p className="migrate-summary">
                  Adds a per-VM DRS override so vCenter will not auto-migrate the new VM after clone. Requires cluster
                  reconfigure permission.
                </p>
              </fieldset>
            ) : null}
          </>
        ) : (
          <>
            <fieldset className="choice">
              <legend>Disk type</legend>
              <label className="check">
                <input type="radio" name="disk" checked={diskProvisioning === ""} onChange={() => setDiskProvisioning("")} />
                Keep current ({diskCounts.thick ? "Thick stays Thick" : "no conversion"})
              </label>
              <label className="check">
                <input
                  type="radio"
                  name="disk"
                  checked={diskProvisioning === "thin"}
                  onChange={() => setDiskProvisioning("thin")}
                />
                Convert to Thin — Storage vMotion; reclaim unused Thick space
              </label>
              <label className="check">
                <input
                  type="radio"
                  name="disk"
                  checked={diskProvisioning === "thick"}
                  onChange={() => setDiskProvisioning("thick")}
                />
                Convert to Thick (lazy zeroed)
              </label>
            </fieldset>

            <fieldset className="choice">
              <legend>Storage location</legend>
              <label className="check">
                <input type="radio" name="storage" checked={!changeStorage} onChange={() => setChangeStorage(false)} />
                Keep current datastore
              </label>
              <label className="check">
                <input type="radio" name="storage" checked={changeStorage} onChange={() => setChangeStorage(true)} />
                Move to another datastore
              </label>
              {changeStorage ? (
                <label>
                  Datastore
                  <select value={datastoreId} onChange={(event) => setDatastoreId(event.target.value)}>
                    <option value="">Select datastore</option>
                    {datastores.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                        {item.free_bytes ? ` · ${Math.round(item.free_bytes / 1024 ** 3)} GiB free` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </fieldset>

            <fieldset className="choice">
              <legend>Networking</legend>
              <label className="check">
                <input type="radio" name="net" checked={!changeNetwork} onChange={() => setChangeNetwork(false)} />
                Keep current networks
              </label>
              <label className="check">
                <input type="radio" name="net" checked={changeNetwork} onChange={() => setChangeNetwork(true)} />
                Connect all NICs to one network
              </label>
              {changeNetwork ? (
                <label>
                  Network
                  <select value={networkId} onChange={(event) => setNetworkId(event.target.value)}>
                    <option value="">Select network</option>
                    {networks.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                        {item.type ? ` · ${item.type}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </fieldset>
          </>
        )}

        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="accent" disabled={busy || !canSubmit}>
            {busy
              ? "Queueing…"
              : mode === "clone"
                ? "Queue clone"
                : mode === "clone_destroy"
                  ? "Queue clone + destroy"
                  : "Queue relocate"}
          </button>
        </div>
      </form>
    </div>
  );
}

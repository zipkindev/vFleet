import { useEffect, useState } from "react";
import { fetchMigrationAccess, grantMigrationAccess } from "./api";
import type { ClusterSummary, MigrationAccessStatus } from "./types";

type Props = {
  clusters: ClusterSummary[];
  jobId?: string;
  onDone?: (message: string) => void;
};

export function MigrationAccessPanel({ clusters, jobId, onDone }: Props) {
  const [clusterId, setClusterId] = useState(clusters[0]?.id ?? "");
  const [status, setStatus] = useState<MigrationAccessStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmScope, setConfirmScope] = useState<"cluster" | "global" | null>(null);

  async function load(id = clusterId) {
    setError("");
    try {
      setStatus(await fetchMigrationAccess(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not read migrate privileges");
    }
  }

  useEffect(() => {
    void load(clusterId);
  }, [clusterId]);

  async function grant(scope: "cluster" | "global") {
    setBusy(true);
    setError("");
    try {
      const result = await grantMigrationAccess({
        confirm: true,
        scope,
        cluster_id: clusterId,
        job_id: jobId,
      });
      setStatus(result);
      setConfirmScope(null);
      onDone?.(result.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not assign migrate role");
    } finally {
      setBusy(false);
    }
  }

  const missing = (status?.privileges ?? []).filter((item) => !item.granted);
  const principal = status?.principal || "the vFleet login";
  const clusterName = status?.cluster_name || clusters.find((item) => item.id === clusterId)?.name || "the cluster";

  return (
    <div className="panel">
      <header>
        <div>
          <h2>vMotion / migration access</h2>
          <p>
            Host migrate is controlled by <strong>vCenter roles</strong>, not a datastore setting. A user who already
            has Administrator (or Authorization.ModifyRoles + ModifyPermissions) can enable it in the vSphere Client
            for everyone (global) or for one account on a cluster.
          </p>
        </div>
      </header>
      {error ? <div className="banner bad">{error}</div> : null}
      {status?.message ? <p className="migrate-summary">{status.message}</p> : null}

      <div className="howto">
        <h3>Manual setup in the vSphere Client</h3>
        <p>
          Sign in as an admin. Current vFleet user: <strong>{principal}</strong>. Privileges to include:
        </p>
        <ul className="howto-privs">
          <li>
            <code>Resource.HotMigrate</code> — vMotion while the VM is on
          </li>
          <li>
            <code>Resource.ColdMigrate</code> — move or Thick→Thin while the VM is off
          </li>
          <li>
            <code>Resource.QueryVMotion</code> — compatibility checks
          </li>
          <li>
            <code>Datastore.Relocate</code> — Storage vMotion / change datastore
          </li>
          <li>
            <code>Datastore.AllocateSpace</code> — allocate space on the destination datastore
          </li>
          <li>
            <code>Network.Assign</code> — only if you remap NICs during migrate
          </li>
        </ul>

        <div className="howto-cols">
          <section>
            <h4>1. Create or edit a role</h4>
            <ol>
              <li>
                Menu → <strong>Administration</strong> → <strong>Access Control</strong> → <strong>Roles</strong>
              </li>
              <li>
                Clone an existing operator role, or click <strong>New</strong> (name it e.g. <code>vFleet-Migrate</code>)
              </li>
              <li>
                Enable the privileges above. They live under <strong>Resource</strong>, <strong>Datastore</strong>, and{" "}
                <strong>Network</strong>
              </li>
              <li>
                Save. Do not use the built-in Read-only role; it cannot be edited
              </li>
            </ol>
          </section>
          <section>
            <h4>2a. One user, one cluster (typical)</h4>
            <ol>
              <li>
                Hosts and Clusters → select cluster <strong>{clusterName}</strong> (or the VM folder)
              </li>
              <li>
                Permissions tab → <strong>Add</strong>
              </li>
              <li>
                User/group: the vFleet service account (<strong>{principal}</strong>), not your personal SSO unless that
                is the login vFleet uses
              </li>
              <li>
                Role: the role from step 1
              </li>
              <li>
                Check <strong>Propagate to children</strong> so VMs inherit it
              </li>
              <li>
                OK, then Recheck below. Cancel any retrying migrate job and queue it again
              </li>
            </ol>
          </section>
          <section>
            <h4>2b. Global (every object this user can see)</h4>
            <ol>
              <li>
                Menu → <strong>Administration</strong> → <strong>Access Control</strong> →{" "}
                <strong>Global Permissions</strong>
              </li>
              <li>
                Add → same user as the vFleet login → the migrate role
              </li>
              <li>
                Check <strong>Propagate to children</strong>
              </li>
              <li>
                This is still per-user (or AD group). It is not a cluster-wide “turn on vMotion for everyone” switch.
                To cover a team, assign an AD/SSO <strong>group</strong> instead of a single user
              </li>
            </ol>
          </section>
        </div>
        <p className="howto-note">
          Host-level vMotion also needs a VMkernel adapter with vMotion enabled on the ESXi hosts. Privilege errors look
          like <code>Resource.ColdMigrate</code> / <code>HotMigrate</code>; a missing vMotion NIC fails later with a
          different fault.
        </p>
      </div>

      <div className="login-grid" style={{ gridTemplateColumns: "1fr auto" }}>
        <label>
          Cluster
          <select value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
            {clusters.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <div className="header-meta-actions" style={{ alignSelf: "end" }}>
          <button className="ghost" type="button" onClick={() => void load()} disabled={busy}>
            Recheck
          </button>
        </div>
      </div>
      <ul className="priv-list">
        {(status?.privileges ?? []).map((item) => (
          <li key={item.id} className={item.granted ? "ok" : "missing"}>
            <strong>{item.granted ? "Granted" : "Missing"}</strong>
            <span>
              {item.label}
              <small className="sub">{item.id}</small>
            </span>
          </li>
        ))}
      </ul>
      <p className="empty">
        Optional: if this vSphere login already has permission-admin rights, the buttons below assign{" "}
        <code>vFleet-Migrate</code> to <strong>{principal}</strong> without opening the vSphere Client.
      </p>
      <div className="modal-actions" style={{ justifyContent: "flex-start" }}>
        <button className="accent" type="button" disabled={busy} onClick={() => setConfirmScope("cluster")}>
          Enable on cluster
        </button>
        <button className="ghost" type="button" disabled={busy} onClick={() => setConfirmScope("global")}>
          Enable globally
        </button>
      </div>
      {missing.length === 0 && status ? <p className="empty">Migrate privileges look present on this object.</p> : null}

      {confirmScope ? (
        <div className="modal-back" onClick={() => !busy && setConfirmScope(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h2>Assign vFleet-Migrate?</h2>
            <p>
              This assigns role <code>vFleet-Migrate</code> to <strong>{status?.principal}</strong> on{" "}
              {confirmScope === "global" ? "the vCenter root folder (all inventory)" : status?.cluster_name || "the cluster"},
              propagating to child VMs. Existing Administrator on that object is left alone. If this login only has
              ModifyPermissions on a subset of the tree, the API call will fail instead of silently escalating.
            </p>
            {jobId ? <p>After grant, the failed migrate job will be requeued.</p> : null}
            <div className="modal-actions">
              <button className="ghost" type="button" disabled={busy} onClick={() => setConfirmScope(null)}>
                Cancel
              </button>
              <button className="accent" type="button" disabled={busy} onClick={() => void grant(confirmScope)}>
                {busy ? "Assigning…" : "Confirm and retry"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function isPermissionJobError(error: string): boolean {
  const text = error.toLowerCase();
  return (
    text.includes("coldmigrate") ||
    text.includes("hotmigrate") ||
    text.includes("datastore.relocate") ||
    text.includes("denied") ||
    text.includes("no permission") ||
    text.includes("nopermission")
  );
}

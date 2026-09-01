import React from "react";
import { cancelJob, clearJobHistory, deleteJob, grantMigrationAccess, retryJob } from "./api";
import { bytes, relTime } from "./format";
import { isPermissionJobError } from "./MigrationAccessPanel";
import { TableFit } from "./TableFit";
import type { Job, JobList } from "./types";

function progressLabel(job: Job): string {
  const sent = Number(job.progress.bytes_sent ?? 0);
  const total = Number(job.progress.bytes_total ?? job.payload.size ?? 0);
  if (job.kind === "upload" && total > 0) {
    return `${bytes(sent)} / ${bytes(Number(total))} (${Math.min(100, Math.round((sent / Number(total)) * 100))}%)`;
  }
  if (job.kind === "migrate" || job.kind === "clone_migrate") {
    const total = Array.isArray(job.payload.vm_ids) ? job.payload.vm_ids.length : job.payload.vm_id ? 1 : 0;
    const index = Number(job.progress.index ?? 0);
    if (job.kind === "clone_migrate") {
      const phase = String(job.progress.phase || "");
      if (phase && phase !== "done") return phase.replace(/_/g, " ");
      return job.status === "succeeded" ? "done" : "running";
    }
    if (total > 0) return `${Math.min(index + (job.status === "succeeded" ? 0 : 1), total)} / ${total}`;
  }
  if (job.kind === "drs_override") {
    const total = Array.isArray(job.payload.vm_ids) ? job.payload.vm_ids.length : 0;
    const index = Number(job.progress.index ?? 0);
    if (total > 0) return `${Math.min(index + (job.status === "succeeded" ? 0 : 1), total)} / ${total}`;
  }
  if (job.kind === "vm_hardware") {
    const total = Array.isArray(job.payload.vm_ids) ? job.payload.vm_ids.length : Number(job.progress.total ?? 0);
    const index = Number(job.progress.index ?? 0);
    const phase = String(job.progress.phase || "");
    const phaseLabels: Record<string, string> = {
      shutdown_request: "Requesting guest shutdown",
      shutdown_wait: "Waiting for guest shutdown",
      force_power_off_start: "Starting forced power-off",
      force_power_off_wait: "Waiting for forced power-off",
      reconfigure_start: "Starting hardware change",
      reconfigure_wait: "Hardware change running",
      reconfigure_complete: "Hardware change complete",
      power_on_start: "Starting VM",
      power_on_wait: "Waiting for VM startup",
    };
    if (phaseLabels[phase]) return phaseLabels[phase];
    if (total > 0) return `${Math.min(index + (job.status === "running" && index < total ? 1 : 0), total)} / ${total}`;
  }
  if (job.kind === "disk_convert") {
    if (job.status !== "running" && job.status !== "queued" && job.status !== "retrying") return job.status;
    const phase = String(job.progress.phase || "");
    const disk = String(job.progress.disk || "disk");
    const index = Number(job.progress.index ?? 0);
    const total = Number(job.progress.total ?? 0);
    if (phase === "ssh_start") return "Starting temporary SSH service";
    if (phase === "clone") return `Cloning ${disk}${total > 0 ? ` (${Math.min(index + 1, total)}/${total})` : ""}`;
    if (phase === "reconfigure") return "Attaching converted disk";
    if (phase === "ssh_stop") return "Restoring SSH service";
    if (phase === "relocate" || job.progress.task_id) return "vSphere task running";
  }
  if (job.progress.task_id) {
    return job.status === "running" ? "vSphere task running" : job.status;
  }
  return job.status;
}

function diskFallbackHint(job: Job): string {
  const plan = job.payload.plan;
  const method = plan && typeof plan === "object" && "method" in plan
    ? String((plan as Record<string, unknown>).method || "")
    : "";
  const directEsxi = String(job.payload._endpoint_kind || "") === "esxi";
  if (job.kind === "disk_convert" && method === "soap" && directEsxi && /not supported/i.test(job.error)) {
    return "This host rejected API relocation. Re-plan from Machines; Automatic uses Verified SSH on direct ESXi.";
  }
  return "";
}

function reconciliationHint(job: Job): string {
  if (job.kind !== "disk_convert" || job.status !== "succeeded") return "";
  const value = job.result.reconciliation;
  if (!value || typeof value !== "object") return "";
  const message = (value as Record<string, unknown>).message;
  return typeof message === "string" ? message : "";
}

function hardwareResultHint(job: Job): string {
  if (job.kind !== "vm_hardware" || !["succeeded", "failed"].includes(job.status)) return "";
  const value = Array.isArray(job.result.results) ? job.result.results : job.progress.results;
  if (!Array.isArray(value)) return "";
  const rows = value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object");
  const failed = rows.filter((row) => !row.ok);
  const summary = `${rows.length - failed.length} succeeded${failed.length ? ` · ${failed.length} failed` : ""}`;
  if (!failed.length) return summary;
  const details = failed.slice(0, 3).map((row) => `${String(row.vm_id || "VM")}: ${String(row.error || "failed")}`).join("; ");
  return `${summary}: ${details}${failed.length > 3 ? "; …" : ""}`;
}

export const JobsView = React.memo(function JobsView({
  data,
  onRefresh,
  onReconcile,
}: {
  data: JobList | null;
  onRefresh: () => void;
  onReconcile?: (job: Job) => void;
}) {
  const jobs = data?.jobs ?? [];
  const terminalCount = jobs.filter((job) => ["succeeded", "failed", "cancelled"].includes(job.status)).length;
  return (
    <div className="panel">
      <header>
        <div>
          <h2>Local relay queue</h2>
          <p>
            Work and history shown here belong only to the current vSphere endpoint. Queued work resumes after a connection
            drop, but endpoint-bound jobs never execute against another saved connection.
          </p>
        </div>
        <div className="header-meta">
          <span>
            {data?.queued ?? 0} queued · {data?.active ?? 0} running
          </span>
          {terminalCount > 0 ? (
            <button
              className="ghost compact"
              onClick={() => {
                if (!window.confirm(`Clear ${terminalCount} completed, failed, or cancelled job${terminalCount === 1 ? "" : "s"} for this endpoint?`)) return;
                void clearJobHistory().then(onRefresh);
              }}
            >
              Clear history
            </button>
          ) : null}
        </div>
      </header>
      {(data?.hidden_other_endpoints ?? 0) > 0 ? (
        <div className="banner">
          <span>
            {data?.hidden_other_endpoints} job{data?.hidden_other_endpoints === 1 ? "" : "s"} from other endpoints hidden. Switch connections to view them.
          </span>{" "}
          <button
            onClick={() => {
              if (!window.confirm("Clear completed, failed, and cancelled history from the hidden endpoints? Current-endpoint, queued, and running jobs will be retained.")) return;
              void clearJobHistory(true).then(onRefresh);
            }}
          >
            Clear hidden history
          </button>
        </div>
      ) : null}
      {jobs.length === 0 ? (
        <p className="empty pad">No jobs yet. Deploy, migrate/clone, or upload an ISO to see the relay at work.</p>
      ) : (
        <TableFit>
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => {
                const fallbackHint = diskFallbackHint(job);
                const reconcileHint = reconciliationHint(job);
                const hardwareHint = hardwareResultHint(job);
                return (
                <tr key={job.id}>
                  <td>
                    {job.title}
                    <small className="sub">{job.kind}</small>
                  </td>
                  <td>
                    <span className={`chip job ${job.status}`}>{job.status}</span>
                    {job.error ? <small className="sub">{job.error}</small> : null}
                    {fallbackHint ? <small className="sub">{fallbackHint}</small> : null}
                    {reconcileHint ? <small className="sub">{reconcileHint}</small> : null}
                    {hardwareHint ? <small className="sub">{hardwareHint}</small> : null}
                    {job.status === "retrying" && job.next_run_at ? (
                      <small className="sub">retry {relTime(job.next_run_at)}</small>
                    ) : null}
                  </td>
                  <td>{progressLabel(job)}</td>
                  <td>{relTime(job.updated_at)}</td>
                  <td className="job-actions">
                    {isPermissionJobError(job.error) && (job.status === "failed" || job.status === "retrying" || job.status === "cancelled") ? (
                      <button
                        className="text"
                        onClick={() => {
                          if (
                            !window.confirm(
                              "Assign the vFleet-Migrate role to this vCenter login on the VM's cluster (propagating), then retry this job? Requires Authorization.ModifyPermissions. Does not grant Administrator.",
                            )
                          ) {
                            return;
                          }
                          void grantMigrationAccess({ confirm: true, scope: "cluster", job_id: job.id }).then(onRefresh);
                        }}
                      >
                        Grant & retry
                      </button>
                    ) : null}
                    {(job.status === "failed" || job.status === "cancelled") && !fallbackHint ? (
                      <button
                        className="text"
                        onClick={() => {
                          void retryJob(job.id).then(onRefresh);
                        }}
                      >
                        Retry
                      </button>
                    ) : null}
                    {job.status === "queued" || job.status === "retrying" ? (
                      <button
                        className="text"
                        onClick={() => {
                          void cancelJob(job.id).then(onRefresh);
                        }}
                      >
                        Cancel
                      </button>
                    ) : null}
                    {job.kind === "disk_convert"
                    && job.status === "succeeded"
                    && job.result.reconciliation
                    && typeof job.result.reconciliation === "object"
                    && (job.result.reconciliation as Record<string, unknown>).status === "action_required" ? (
                      <button className="text" onClick={() => onReconcile?.(job)}>
                        Reconcile
                      </button>
                    ) : null}
                    {["succeeded", "failed", "cancelled"].includes(job.status) ? (
                      <button
                        className="text danger-text"
                        onClick={() => {
                          void deleteJob(job.id).then(onRefresh);
                        }}
                      >
                        Remove
                      </button>
                    ) : null}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </TableFit>
      )}
    </div>
  );
});

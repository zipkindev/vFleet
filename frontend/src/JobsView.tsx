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
  if (job.kind === "disk_convert") {
    if (job.status !== "running" && job.status !== "queued" && job.status !== "retrying") return job.status;
    const phase = String(job.progress.phase || "");
    const disk = String(job.progress.disk || "disk");
    const index = Number(job.progress.index ?? 0);
    const total = Number(job.progress.total ?? 0);
    if (phase === "clone") return `Cloning ${disk}${total > 0 ? ` (${Math.min(index + 1, total)}/${total})` : ""}`;
    if (phase === "reconfigure") return "Attaching converted disk";
    if (phase === "relocate" || job.progress.task_id) return "vSphere task running";
  }
  if (job.progress.task_id) {
    return job.status === "running" ? "vSphere task running" : job.status;
  }
  return job.status;
}

function diskFallbackHint(job: Job): string {
  const plan = job.payload.plan;
  const fallback = plan && typeof plan === "object" && "fallback_method" in plan
    ? String((plan as Record<string, unknown>).fallback_method || "")
    : "";
  if (job.kind === "disk_convert" && fallback === "ssh" && /not supported/i.test(job.error)) {
    return "This host rejected API relocation. Re-plan from Machines and choose Verified SSH fallback.";
  }
  return "";
}

export const JobsView = React.memo(function JobsView({ data, onRefresh }: { data: JobList | null; onRefresh: () => void }) {
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

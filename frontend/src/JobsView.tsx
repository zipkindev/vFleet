import { cancelJob, grantMigrationAccess, retryJob } from "./api";
import { bytes, relTime } from "./format";
import { isPermissionJobError } from "./MigrationAccessPanel";
import type { Job, JobList } from "./types";

function progressLabel(job: Job): string {
  const sent = Number(job.progress.bytes_sent ?? 0);
  const total = Number(job.progress.bytes_total ?? job.payload.size ?? 0);
  if (job.kind === "upload" && total > 0) {
    return `${bytes(sent)} / ${bytes(Number(total))} (${Math.min(100, Math.round((sent / Number(total)) * 100))}%)`;
  }
  if (job.kind === "migrate") {
    const total = Array.isArray(job.payload.vm_ids) ? job.payload.vm_ids.length : 0;
    const index = Number(job.progress.index ?? 0);
    if (total > 0) return `${Math.min(index + (job.status === "succeeded" ? 0 : 1), total)} / ${total}`;
  }
  if (job.progress.task_id) return "vCenter task running";
  return job.status;
}

export function JobsView({ data, onRefresh }: { data: JobList | null; onRefresh: () => void }) {
  const jobs = data?.jobs ?? [];
  return (
    <div className="panel">
      <header>
        <div>
          <h2>Local relay queue</h2>
          <p>
            Work is stored on this machine. If the VPN drops, queued clones, uploads, migrates, and power actions resume
            from the last checkpoint instead of starting over. Permission errors can be granted from Datastores → vMotion
            / migration access, or with Grant & retry on the job.
          </p>
        </div>
        <div className="header-meta">
          <span>
            {data?.queued ?? 0} queued · {data?.active ?? 0} running
          </span>
        </div>
      </header>
      {jobs.length === 0 ? (
        <p className="empty pad">No jobs yet. Clone, migrate, or upload an ISO to see the relay at work.</p>
      ) : (
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
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  {job.title}
                  <small className="sub">{job.kind}</small>
                </td>
                <td>
                  <span className={`chip job ${job.status}`}>{job.status}</span>
                  {job.error ? <small className="sub">{job.error}</small> : null}
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
                  {job.status === "failed" || job.status === "cancelled" ? (
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
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

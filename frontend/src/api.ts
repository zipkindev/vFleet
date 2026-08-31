import type {
  ActionName,
  ActionResult,
  Catalog,
  ConnectionInfo,
  ConnectionProfileList,
  ConsoleTicket,
  DatastoreListing,
  DiskConversionPlan,
  HostManagementInfo,
  InventorySnapshot,
  Job,
  JobList,
  MetricsResponse,
  MigrationAccessStatus,
  StagingSession,
  VmFolder,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function tokenHeader(): Record<string, string> {
  const token = localStorage.getItem("vfleet.uiToken") || "";
  return token ? { "X-UI-Token": token } : {};
}

async function readError(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const payload = JSON.parse(text) as { detail?: string | Array<{ msg: string }> };
    if (typeof payload.detail === "string") return sanitizeErrorText(payload.detail);
    if (Array.isArray(payload.detail) && payload.detail[0]?.msg) return sanitizeErrorText(payload.detail[0].msg);
  } catch {
    /* keep raw text */
  }
  return sanitizeErrorText(text) || `Request failed (${response.status})`;
}

function sanitizeErrorText(raw: string): string {
  const text = raw.trim();
  if (!text) return text;
  const lower = text.toLowerCase();
  if (lower.includes("<html") || lower.includes("<!doctype") || lower.includes("nginx/")) {
    if (/\b404\b/.test(text) || lower.includes("404 not found")) {
      return "vCenter returned HTTP 404 for the API path (the UI may still work). Check host/port or retry.";
    }
    const title = text.match(/<title>([^<]+)<\/title>/i)?.[1]?.replace(/\s+/g, " ").trim();
    return title ? `vCenter returned an HTML error (${title})` : "vCenter returned an HTML error page instead of the API";
  }
  if (lower.includes("session is not authenticated") || lower.includes("notauthenticated")) {
    return "vCenter API session expired. Reconnecting; use Connect if inventory stays stale.";
  }
  const soapMsg = text.match(/\bmsg\s*=\s*'([^']+)'/i);
  if (soapMsg && (lower.includes("vim.fault") || lower.includes("vmodl."))) {
    return soapMsg[1].replace(/\.\s*$/, "");
  }
  return text.length > 240 ? `${text.slice(0, 237)}…` : text;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  Object.entries(tokenHeader()).forEach(([key, value]) => headers.set(key, value));
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.json() as Promise<T>;
}

export async function fetchInventory(params: Record<string, string> = {}): Promise<InventorySnapshot> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query}` : "";
  return request<InventorySnapshot>(`/api/inventory${suffix}`);
}

export async function fetchConnection(): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/connection");
}

export async function fetchConnectionProfiles(): Promise<ConnectionProfileList> {
  return request<ConnectionProfileList>("/api/connection-profiles");
}

export async function connectProfile(profileId: string): Promise<ConnectionInfo> {
  return request<ConnectionInfo>(`/api/connection-profiles/${encodeURIComponent(profileId)}/connect`, { method: "POST" });
}

export async function deleteConnectionProfile(profileId: string): Promise<void> {
  await request<{ deleted: boolean }>(`/api/connection-profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm: true }),
  });
}

export async function login(body: {
  host: string;
  user: string;
  password: string;
  port: number;
  insecure: boolean;
  remember: boolean;
  connect: boolean;
  endpoint_kind?: string;
  ssh_enabled?: boolean;
  ssh_user?: string;
  ssh_password?: string;
  ssh_port?: number;
  ssh_host_key_sha256?: string;
  profile_id?: string;
  profile_name?: string;
}): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/login", { method: "POST", body: JSON.stringify(body) });
}

export async function logout(forget = false): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/logout", { method: "POST", body: JSON.stringify({ forget }) });
}

export async function runActions(vmIds: string[], action: ActionName): Promise<ActionResult[]> {
  const payload = await request<{ results: ActionResult[]; job_id?: string; queued?: boolean }>("/api/actions", {
    method: "POST",
    body: JSON.stringify({ vm_ids: vmIds, action, confirm: true }),
  });
  return payload.results;
}

export async function fetchConsoleTicket(vmId: string, type: "vmrc" | "webmks" = "vmrc"): Promise<ConsoleTicket> {
  return request<ConsoleTicket>(`/api/vms/${encodeURIComponent(vmId)}/console`, {
    method: "POST",
    body: JSON.stringify({ type }),
  });
}

export function vcenterVmConsoleUrl(host: string, vmId: string, port = 443): string {
  const authority = !host || port === 443 ? host : `${host}:${port}`;
  return `https://${authority}/ui/app/vm;nav=s/urn:vmomi:VirtualMachine:${vmId}/console`;
}

export const VMRC_INSTALL_URL =
  "https://support.broadcom.com/group/ecx/productdownloads?subfamily=VMware%20Remote%20Console";

/** Launch vmrc:// and other custom protocol URIs without leaving a blank browser tab. */
export function launchExternalUri(uri: string): void {
  if (/^[a-z][a-z0-9+.-]*:/i.test(uri) && !/^https?:/i.test(uri)) {
    const link = document.createElement("a");
    link.href = uri;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    return;
  }
  window.open(uri, "_blank", "noopener,noreferrer");
}

export async function fetchCatalog(): Promise<Catalog> {
  return request<Catalog>("/api/catalog");
}

export async function listVmFolders(datacenter = ""): Promise<VmFolder[]> {
  const query = datacenter ? `?datacenter=${encodeURIComponent(datacenter)}` : "";
  return request<VmFolder[]>(`/api/folders${query}`);
}

export async function fetchMetrics(
  params: { hours?: number; owner?: string; host?: string; datastore?: string; since?: string; until?: string } = {},
): Promise<MetricsResponse> {
  const query = new URLSearchParams();
  if (params.hours !== undefined) query.set("hours", String(params.hours));
  if (params.owner) query.set("owner", params.owner);
  if (params.host) query.set("host", params.host);
  if (params.datastore) query.set("datastore", params.datastore);
  if (params.since) query.set("since", params.since);
  if (params.until) query.set("until", params.until);
  const suffix = query.toString() ? `?${query}` : "";
  return request<MetricsResponse>(`/api/metrics${suffix}`);
}

export async function fetchDatastoreFiles(datastoreId: string, path = ""): Promise<DatastoreListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return request<DatastoreListing>(`/api/datastores/${encodeURIComponent(datastoreId)}/files${query}`);
}

export async function fetchJobs(): Promise<JobList> {
  return request<JobList>("/api/jobs");
}

export async function cancelJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function retryJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/retry`, { method: "POST" });
}

export async function deleteJob(jobId: string): Promise<void> {
  await request<{ deleted: boolean }>(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export async function clearJobHistory(otherEndpoints = false): Promise<number> {
  const result = await request<{ deleted: number }>("/api/jobs/history", {
    method: "DELETE",
    body: JSON.stringify({ confirm: true, other_endpoints: otherEndpoints }),
  });
  return result.deleted;
}

export async function fetchMigrationAccess(clusterId = ""): Promise<MigrationAccessStatus> {
  const suffix = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : "";
  return request<MigrationAccessStatus>(`/api/migration-access${suffix}`);
}

export async function grantMigrationAccess(body: {
  confirm: boolean;
  scope: "cluster" | "global";
  cluster_id?: string;
  job_id?: string;
}): Promise<MigrationAccessStatus> {
  return request<MigrationAccessStatus>("/api/migration-access", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function cloneVm(body: {
  template_id: string;
  name: string;
  datastore_id: string;
  cluster_id?: string;
  host_id?: string;
  folder_id?: string;
  disable_drs?: boolean;
  cpu_count?: number;
  memory_mib?: number;
  power_on?: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms", { method: "POST", body: JSON.stringify(body) });
}

export async function deployFromTemplate(body: {
  template_id: string;
  name: string;
  datastore_id: string;
  cluster_id?: string;
  host_id?: string;
  folder_id?: string;
  disable_drs?: boolean;
  cpu_count?: number;
  memory_mib?: number;
  power_on?: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/deploy", { method: "POST", body: JSON.stringify(body) });
}

export async function renameVm(vmId: string, name: string): Promise<Job> {
  return request<Job>(`/api/vms/${encodeURIComponent(vmId)}/rename`, {
    method: "POST",
    body: JSON.stringify({ name, confirm: true }),
  });
}

export async function applyDrsOverride(vmIds: string[]): Promise<Job> {
  return request<Job>("/api/vms/drs-override", {
    method: "POST",
    body: JSON.stringify({ vm_ids: vmIds, confirm: true }),
  });
}

export async function migrateVms(body: {
  vm_ids: string[];
  host_id?: string;
  datastore_id?: string;
  network_id?: string;
  disk_provisioning?: string;
  folder_id?: string;
  confirm: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/migrate", { method: "POST", body: JSON.stringify(body) });
}

export async function planDiskConversion(body: {
  vm_id: string;
  target: string;
  method: string;
}): Promise<DiskConversionPlan> {
  return request<DiskConversionPlan>("/api/vms/disk-conversion/plan", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function convertVmDisks(plan: DiskConversionPlan): Promise<Job> {
  return request<Job>("/api/vms/disk-conversion", {
    method: "POST",
    body: JSON.stringify({
      vm_id: plan.vm_id,
      target: plan.target,
      method: plan.method,
      plan_token: plan.plan_token,
      confirm: true,
    }),
  });
}

export async function fetchHostManagement(): Promise<HostManagementInfo> {
  return request<HostManagementInfo>("/api/host");
}

export async function queueHostAction(action: string): Promise<Job> {
  return request<Job>("/api/host/actions", {
    method: "POST",
    body: JSON.stringify({ action, confirm: true, timeout_seconds: 900 }),
  });
}

export async function queueHostService(service_key: string, action: string, policy = ""): Promise<Job> {
  return request<Job>("/api/host/services", {
    method: "POST",
    body: JSON.stringify({ service_key, action, policy, confirm: true }),
  });
}

export async function queueHostTime(ntp_servers: string[], sync_now: boolean): Promise<Job> {
  return request<Job>("/api/host/time", {
    method: "POST",
    body: JSON.stringify({ ntp_servers, sync_now, confirm: true }),
  });
}

export async function queueStorageRescan(): Promise<Job> {
  return request<Job>("/api/host/storage/rescan", {
    method: "POST",
    body: JSON.stringify({ action: "rescan", confirm: true }),
  });
}

export async function queueSupportBundle(): Promise<Job> {
  return request<Job>("/api/host/support-bundle", {
    method: "POST",
    body: JSON.stringify({ action: "support_bundle", confirm: true }),
  });
}

export async function cloneMigrate(body: {
  vm_id: string;
  host_id: string;
  name?: string;
  destroy_source?: boolean;
  disable_drs?: boolean;
  datastore_id?: string;
  folder_id?: string;
  power_on?: boolean;
  confirm: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/clone-migrate", { method: "POST", body: JSON.stringify(body) });
}

export async function mkdir(datastoreId: string, path: string): Promise<Job> {
  return request<Job>("/api/datastores/mkdir", {
    method: "POST",
    body: JSON.stringify({ datastore_id: datastoreId, path }),
  });
}

export async function deleteDatastoreFile(datastoreId: string, path: string): Promise<Job> {
  return request<Job>("/api/datastores/delete", {
    method: "POST",
    body: JSON.stringify({ datastore_id: datastoreId, path, confirm: true }),
  });
}

export async function createStaging(filename: string, size: number): Promise<StagingSession> {
  return request<StagingSession>("/api/staging", {
    method: "POST",
    body: JSON.stringify({ filename, size }),
  });
}

export async function putStagingChunk(id: string, blob: Blob, start: number, total: number): Promise<StagingSession> {
  const headers = new Headers(tokenHeader());
  const end = start + blob.size - 1;
  headers.set("Content-Range", `bytes ${start}-${end}/${total}`);
  headers.set("Content-Type", "application/octet-stream");
  const response = await fetch(`/api/staging/${id}`, { method: "PUT", headers, body: blob });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.json() as Promise<StagingSession>;
}

export async function enqueueUpload(body: {
  staging_id: string;
  datastore_id: string;
  remote_path: string;
  use_library: boolean;
}): Promise<Job> {
  return request<Job>("/api/uploads", { method: "POST", body: JSON.stringify(body) });
}

export function setUiToken(token: string) {
  if (token) localStorage.setItem("vfleet.uiToken", token);
  else localStorage.removeItem("vfleet.uiToken");
}

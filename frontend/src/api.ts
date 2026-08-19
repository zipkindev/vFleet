import type {
  ActionName,
  ActionResult,
  Catalog,
  ConnectionInfo,
  DatastoreListing,
  InventorySnapshot,
  Job,
  JobList,
  MetricsResponse,
  MigrationAccessStatus,
  StagingSession,
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

export async function login(body: {
  host: string;
  user: string;
  password: string;
  port: number;
  insecure: boolean;
  remember: boolean;
  connect: boolean;
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

export async function fetchCatalog(): Promise<Catalog> {
  return request<Catalog>("/api/catalog");
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
  cpu_count?: number;
  memory_mib?: number;
  power_on?: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms", { method: "POST", body: JSON.stringify(body) });
}

export async function migrateVms(body: {
  vm_ids: string[];
  host_id?: string;
  datastore_id?: string;
  network_id?: string;
  disk_provisioning?: string;
  confirm: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/migrate", { method: "POST", body: JSON.stringify(body) });
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

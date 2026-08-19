from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .adapters import build_adapter
from .adapters.base import InventoryAdapter
from .adapters.demo import DemoAdapter
from .adapters.vcenter import VCenterAdapter
from .config import settings
from .models import (
    ActionRequest,
    ActionResponse,
    ActionResult,
    Catalog,
    CloneVmRequest,
    ConnectionInfo,
    DatastoreListing,
    DeleteFileRequest,
    InventorySnapshot,
    Job,
    JobList,
    LoginRequest,
    LogoutRequest,
    MetricsResponse,
    MigrateVmRequest,
    GrantMigrationRequest,
    MigrationAccessStatus,
    MkdirRequest,
    StagingCreateRequest,
    StagingSession,
    UploadRequest,
)
from .reclaim import build_owner_reports
from .metrics import build_owner_utilization
from .power import normalize_power_state
from .relay import RelayWorker
from .session import apply_runtime, forget_vcenter, parse_endpoint, persist_vcenter
from .store import LocalStore
from .vm_storage import normalize_disk_transform

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
ALLOWED_ACTIONS = {"start", "shutdown", "power_off", "reboot", "reset", "suspend", "destroy"}


def get_adapter(request: Request) -> InventoryAdapter:
    return request.app.state.adapter


def get_store(request: Request) -> LocalStore:
    return request.app.state.store


def get_worker(request: Request) -> RelayWorker:
    return request.app.state.worker


def require_token(x_ui_token: Optional[str] = Header(default=None, alias="X-UI-Token")) -> None:
    expected = settings.ui_token.strip()
    if not expected:
        return
    if x_ui_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-UI-Token")


def _close(adapter: object) -> None:
    closer = getattr(adapter, "close", None)
    if callable(closer):
        closer()


def _source(request: Request) -> str:
    return getattr(request.app.state, "session_source", "demo")


def decorate_connection(conn: ConnectionInfo, request: Request) -> ConnectionInfo:
    worker: RelayWorker = request.app.state.worker
    decorated = worker.overlay(conn)
    return decorated.model_copy(
        update={
            "source": _source(request),
            "env_ready": settings.has_vcenter_creds,
            "saved_host": settings.vcenter_host,
            "saved_user": settings.vcenter_user,
            "saved_port": settings.vcenter_port,
            "insecure": settings.vcenter_insecure,
            "can_disconnect": conn.mode == "vcenter",
        }
    )


def _filter_snapshot(snapshot: InventorySnapshot, q, owner, cluster, power, prefix) -> InventorySnapshot:
    vms = snapshot.vms
    if q:
        needle = q.lower()
        vms = [vm for vm in vms if needle in vm.name.lower() or needle in vm.owner_key.lower()]
    if prefix:
        vms = [vm for vm in vms if vm.name.lower().startswith(prefix.lower())]
    if owner:
        vms = [vm for vm in vms if vm.owner_key.lower() == owner.lower()]
    if cluster:
        vms = [vm for vm in vms if cluster.lower() in (vm.cluster_id.lower(), vm.cluster_name.lower())]
    if power:
        want = normalize_power_state(power)
        vms = [vm for vm in vms if normalize_power_state(vm.power_state) == want]
    if cluster:
        needle = cluster.lower()
        hosts = [
            item
            for item in snapshot.hosts
            if needle in (item.cluster_id.lower(), item.cluster_name.lower())
        ]
        clusters = [
            item
            for item in snapshot.clusters
            if needle in (item.id.lower(), item.name.lower())
        ]
    else:
        hosts = list(snapshot.hosts)
        clusters = list(snapshot.clusters)
    return InventorySnapshot(
        connection=snapshot.connection,
        clusters=clusters,
        hosts=hosts,
        vms=vms,
        owners=build_owner_reports(vms),
    )


def _enqueue(request: Request, kind: str, title: str, payload: dict, idempotency_key: str = "") -> Job:
    store: LocalStore = request.app.state.store
    worker: RelayWorker = request.app.state.worker
    job = store.enqueue(kind, title, payload, idempotency_key=idempotency_key)
    worker.wake()
    return job


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = LocalStore(settings.data_dir / "vfleet.db")
    adapter = build_adapter(settings)
    app.state.store = store
    app.state.adapter = adapter
    app.state.session_source = settings.resolved_mode if settings.resolved_mode == "vcenter" else "demo"
    app.state.swap_lock = threading.Lock()
    worker = RelayWorker(settings, store, lambda: app.state.adapter)
    app.state.worker = worker
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        _close(adapter)
        store.close()


APP_VERSION = "1.0.0"

app = FastAPI(title="vFleet", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health(request: Request) -> dict:
    adapter = getattr(request.app.state, "adapter", None)
    from .adapters.demo import DemoAdapter

    if adapter is None:
        mode = settings.resolved_mode
    elif isinstance(adapter, DemoAdapter):
        mode = "demo"
    else:
        mode = "vcenter"
    counts = request.app.state.store.counts() if hasattr(request.app.state, "store") else {}
    return {"ok": True, "mode": mode, "env_ready": settings.has_vcenter_creds, **counts}


@app.get("/api/version")
def version() -> dict:
    return {"version": APP_VERSION}


@app.get("/api/connection", response_model=ConnectionInfo, dependencies=[Depends(require_token)])
def connection(request: Request, adapter: InventoryAdapter = Depends(get_adapter)) -> ConnectionInfo:
    from .adapters.demo import DemoAdapter

    worker: RelayWorker = request.app.state.worker
    if isinstance(adapter, DemoAdapter):
        info = adapter.connection()
    else:
        info = ConnectionInfo(
            mode="vcenter",
            connected=worker.reachable is not False and not worker.last_error,
            host=settings.vcenter_host,
            user=settings.vcenter_user,
            message=worker.last_error or "Local relay",
        )
    return decorate_connection(info, request)


@app.post("/api/login", response_model=ConnectionInfo, dependencies=[Depends(require_token)])
def login(body: LoginRequest, request: Request) -> ConnectionInfo:
    host = body.host.strip()
    user = body.user.strip()
    password = body.password
    if not host or not user or not password:
        raise HTTPException(status_code=400, detail="Host, username, and password are required")
    try:
        endpoint, port = parse_endpoint(host, body.port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    live = apply_runtime(settings, endpoint, user, password, port, body.insecure)
    candidate = VCenterAdapter(live)
    info = candidate.connection()
    if not info.connected:
        _close(candidate)
        raise HTTPException(status_code=401, detail=info.message or "Could not sign in to vCenter")

    with request.app.state.swap_lock:
        previous = request.app.state.adapter
        request.app.state.adapter = candidate
        request.app.state.session_source = "ui"
        _close(previous)

    if body.remember:
        persist_vcenter(settings, endpoint, user, password, port, body.insecure)
        info.message = f"{info.message}. Saved on this machine for the next start."
    request.app.state.worker.wake()
    return decorate_connection(info, request)


@app.post("/api/logout", response_model=ConnectionInfo, dependencies=[Depends(require_token)])
def logout(request: Request, body: Optional[LogoutRequest] = None) -> ConnectionInfo:
    payload = body or LogoutRequest()
    with request.app.state.swap_lock:
        previous = request.app.state.adapter
        request.app.state.adapter = DemoAdapter(settings)
        request.app.state.session_source = "demo"
        _close(previous)
    if payload.forget:
        forget_vcenter(settings)
    request.app.state.worker.wake()
    return decorate_connection(request.app.state.adapter.connection(), request)


@app.get("/api/inventory", response_model=InventorySnapshot, dependencies=[Depends(require_token)])
def inventory(
    request: Request,
    adapter: InventoryAdapter = Depends(get_adapter),
    worker: RelayWorker = Depends(get_worker),
    q: Optional[str] = Query(default=None),
    owner: Optional[str] = Query(default=None),
    cluster: Optional[str] = Query(default=None),
    power: Optional[str] = Query(default=None),
    prefix: Optional[str] = Query(default=None),
) -> InventorySnapshot:
    snapshot = worker.cached_snapshot()
    if snapshot is None:
        try:
            snapshot = adapter.snapshot()
            request.app.state.store.save_inventory(snapshot)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"No local inventory yet: {exc}") from exc
        snapshot.connection = decorate_connection(snapshot.connection, request)
    else:
        snapshot.connection = decorate_connection(snapshot.connection, request)
    return _filter_snapshot(snapshot, q, owner, cluster, power, prefix)


@app.get("/api/metrics", response_model=MetricsResponse, dependencies=[Depends(require_token)])
def metrics(
    request: Request,
    worker: RelayWorker = Depends(get_worker),
    hours: float = Query(default=24.0, ge=0.25, le=168.0),
    owner: Optional[str] = Query(default=None),
) -> MetricsResponse:
    store: LocalStore = request.app.state.store
    owner_key = (owner or "").strip()
    series = store.load_metrics(hours=hours, owner=owner_key)
    snapshot = worker.cached_snapshot()
    catalog = store.load_catalog()
    owners: list = []
    if snapshot is not None:
        owners = build_owner_utilization(snapshot, catalog)
    return MetricsResponse(
        series=series,
        owners=owners,
        hours=hours,
        owner=owner_key,
        points=len(series),
    )


@app.get("/api/catalog", response_model=Catalog, dependencies=[Depends(require_token)])
def catalog(request: Request, adapter: InventoryAdapter = Depends(get_adapter)) -> Catalog:
    cached = request.app.state.store.load_catalog()
    worker: RelayWorker = request.app.state.worker
    if cached is not None:
        snap = worker.cached_snapshot()
        stale = bool(worker.last_error) or bool(snap and snap.connection.stale)
        return cached.model_copy(
            update={
                "stale": stale,
                "last_sync": request.app.state.store.inventory_saved_at() or cached.last_sync,
            }
        )
    try:
        networks = []
        try:
            networks = adapter.list_networks()
        except Exception:
            networks = []
        data = Catalog(
            datastores=adapter.list_datastores(),
            templates=adapter.list_templates(),
            networks=networks,
        )
        request.app.state.store.save_catalog(data)
        return data
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/datastores/{datastore_id}/files", response_model=DatastoreListing, dependencies=[Depends(require_token)])
def datastore_files(
    datastore_id: str,
    request: Request,
    adapter: InventoryAdapter = Depends(get_adapter),
    path: str = Query(default=""),
) -> DatastoreListing:
    store: LocalStore = request.app.state.store
    try:
        listing = adapter.browse_datastore(datastore_id, path)
        store.save_browse(datastore_id, path, listing.model_dump_json())
        return listing
    except Exception as exc:
        cached = store.load_browse(datastore_id, path)
        if cached:
            listing = DatastoreListing.model_validate_json(cached)
            listing.stale = True
            return listing
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/actions", response_model=ActionResponse, dependencies=[Depends(require_token)])
def actions(body: ActionRequest, request: Request) -> ActionResponse:
    action = body.action.strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported action. Use one of: {sorted(ALLOWED_ACTIONS)}")
    if not body.vm_ids:
        raise HTTPException(status_code=400, detail="No virtual machines selected")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to run this operation")
    if len(body.vm_ids) > 50:
        raise HTTPException(
            status_code=400,
            detail="Refusing more than 50 VMs in one request (local relay batch limit; split into a second job)",
        )
    job = _enqueue(
        request,
        "power",
        f"{action} {len(body.vm_ids)} VM(s)",
        {"vm_ids": body.vm_ids, "action": action},
    )
    return ActionResponse(
        results=[
            ActionResult(vm_id=vm_id, name="", action=action, ok=True, message="Queued locally")
            for vm_id in body.vm_ids
        ],
        job_id=job.id,
        queued=True,
    )


@app.post("/api/vms", response_model=Job, dependencies=[Depends(require_token)])
def clone_vm(body: CloneVmRequest, request: Request) -> Job:
    name = body.name.strip()
    if not name or "/" in name:
        raise HTTPException(status_code=400, detail="VM name is required and cannot contain '/'")
    if not body.template_id or not body.datastore_id:
        raise HTTPException(status_code=400, detail="Template and datastore are required")
    if body.cpu_count is not None and not 1 <= body.cpu_count <= 128:
        raise HTTPException(status_code=400, detail="cpu_count must be between 1 and 128")
    if body.memory_mib is not None and not 128 <= body.memory_mib <= 1_048_576:
        raise HTTPException(status_code=400, detail="memory_mib is out of range")
    return _enqueue(
        request,
        "clone",
        f"Clone {name}",
        body.model_dump(),
        idempotency_key=f"clone:{name}",
    )


@app.post("/api/vms/migrate", response_model=Job, dependencies=[Depends(require_token)])
def migrate_vms(body: MigrateVmRequest, request: Request) -> Job:
    vm_ids = [item.strip() for item in body.vm_ids if item and item.strip()]
    if not vm_ids:
        raise HTTPException(status_code=400, detail="No virtual machines selected")
    if len(vm_ids) > 50:
        raise HTTPException(
            status_code=400,
            detail="Refusing more than 50 VMs in one request (local relay batch limit; split into a second job)",
        )
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to migrate")
    try:
        disk = normalize_disk_transform(body.disk_provisioning)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    host_id = (body.host_id or "").strip()
    datastore_id = (body.datastore_id or "").strip()
    network_id = (body.network_id or "").strip()
    if not host_id and not datastore_id and not network_id and not disk:
        raise HTTPException(
            status_code=400,
            detail="Pick a destination host, datastore, network, or disk type",
        )
    snapshot = request.app.state.worker.cached_snapshot()
    host_name = host_id
    if snapshot is not None and host_id:
        host = next((item for item in snapshot.hosts if item.id == host_id), None)
        if host is None:
            raise HTTPException(status_code=400, detail="Unknown host")
        host_name = host.name
        selected = [vm for vm in snapshot.vms if vm.id in set(vm_ids)]
        wrong = [vm.name for vm in selected if vm.cluster_id and vm.cluster_id != host.cluster_id]
        if wrong:
            raise HTTPException(
                status_code=400,
                detail="All VMs must be in the destination host's cluster: " + ", ".join(wrong[:8]),
            )
    parts = [f"{len(vm_ids)} VM(s)"]
    if host_id:
        parts.append(f"→ {host_name}")
    if disk:
        parts.append(f"{disk} disks")
    payload = body.model_dump()
    payload.update(
        {
            "vm_ids": vm_ids,
            "host_id": host_id,
            "datastore_id": datastore_id,
            "network_id": network_id,
            "disk_provisioning": disk,
        }
    )
    return _enqueue(
        request,
        "migrate",
        "Migrate " + " ".join(parts),
        payload,
        idempotency_key="migrate:" + ",".join(sorted(vm_ids)) + f":{host_id}:{datastore_id}:{network_id}:{disk}",
    )


@app.post("/api/datastores/mkdir", response_model=Job, dependencies=[Depends(require_token)])
def mkdir(body: MkdirRequest, request: Request) -> Job:
    path = body.path.strip().strip("/")
    if not path:
        raise HTTPException(status_code=400, detail="Folder path is required")
    return _enqueue(
        request,
        "mkdir",
        f"Create folder {path}",
        {"datastore_id": body.datastore_id, "path": path},
        idempotency_key=f"mkdir:{body.datastore_id}:{path}",
    )


@app.post("/api/datastores/delete", response_model=Job, dependencies=[Depends(require_token)])
def delete_file(body: DeleteFileRequest, request: Request) -> Job:
    path = body.path.strip().strip("/")
    if not path:
        raise HTTPException(status_code=400, detail="Path is required")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to delete a datastore file")
    return _enqueue(
        request,
        "delete_file",
        f"Delete {path}",
        {"datastore_id": body.datastore_id, "path": path},
    )


@app.post("/api/staging", response_model=StagingSession, dependencies=[Depends(require_token)])
def create_staging(body: StagingCreateRequest, store: LocalStore = Depends(get_store)) -> StagingSession:
    if body.size < 0 or not body.filename.strip():
        raise HTTPException(status_code=400, detail="filename and size are required")
    return store.create_staging(body.filename.strip(), body.size)


@app.put("/api/staging/{staging_id}", response_model=StagingSession, dependencies=[Depends(require_token)])
async def put_staging(
    staging_id: str,
    request: Request,
    store: LocalStore = Depends(get_store),
    content_range: Optional[str] = Header(default=None),
) -> StagingSession:
    session = store.staging(staging_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown staging upload")
    data = await request.body()
    start = session.received
    if content_range and content_range.lower().startswith("bytes "):
        spec = content_range.split(" ", 1)[1]
        span = spec.split("/")[0]
        start = int(span.split("-", 1)[0])
    return store.write_staging_chunk(staging_id, start, data)


@app.post("/api/uploads", response_model=Job, dependencies=[Depends(require_token)])
def uploads(body: UploadRequest, request: Request, store: LocalStore = Depends(get_store)) -> Job:
    session = store.staging(body.staging_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown staging upload")
    if not session.complete:
        raise HTTPException(status_code=400, detail="Finish uploading the file to this machine first")
    remote = body.remote_path.strip().strip("/")
    if not remote:
        raise HTTPException(status_code=400, detail="remote_path is required")
    return _enqueue(
        request,
        "upload",
        f"Upload {session.filename} → {remote}",
        {
            "staging_id": session.id,
            "datastore_id": body.datastore_id,
            "remote_path": remote,
            "local_path": str(store.staging_path(session.id)),
            "use_library": body.use_library,
            "size": session.size,
        },
        idempotency_key=f"upload:{body.datastore_id}:{remote}",
    )


@app.get("/api/jobs", response_model=JobList, dependencies=[Depends(require_token)])
def jobs(store: LocalStore = Depends(get_store)) -> JobList:
    rows = store.list_jobs()
    counts = store.counts()
    return JobList(jobs=rows, queued=counts["queued"], active=counts["active"])


@app.get("/api/jobs/{job_id}", response_model=Job, dependencies=[Depends(require_token)])
def job_detail(job_id: str, store: LocalStore = Depends(get_store)) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/api/jobs/{job_id}/cancel", response_model=Job, dependencies=[Depends(require_token)])
def cancel_job(job_id: str, store: LocalStore = Depends(get_store)) -> Job:
    job = store.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/api/jobs/{job_id}/retry", response_model=Job, dependencies=[Depends(require_token)])
def retry_job(job_id: str, request: Request) -> Job:
    store: LocalStore = request.app.state.store
    job = store.requeue(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    request.app.state.worker.wake()
    return job


@app.get("/api/migration-access", response_model=MigrationAccessStatus, dependencies=[Depends(require_token)])
def migration_access(
    adapter: InventoryAdapter = Depends(get_adapter),
    cluster_id: str = Query(default=""),
) -> MigrationAccessStatus:
    try:
        payload = adapter.migration_access(cluster_id.strip())
        return MigrationAccessStatus.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/migration-access", response_model=MigrationAccessStatus, dependencies=[Depends(require_token)])
def grant_migration_access(body: GrantMigrationRequest, request: Request) -> MigrationAccessStatus:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true to assign the vFleet-Migrate role to this login",
        )
    scope = (body.scope or "cluster").strip().lower()
    if scope not in {"cluster", "global"}:
        raise HTTPException(status_code=400, detail="scope must be cluster or global")
    cluster_id = (body.cluster_id or "").strip()
    job_id = (body.job_id or "").strip()
    if scope == "cluster" and not cluster_id and job_id:
        store: LocalStore = request.app.state.store
        job = store.get(job_id)
        snapshot = request.app.state.worker.cached_snapshot()
        vm_ids = set((job.payload.get("vm_ids") if job else None) or [])
        if snapshot and vm_ids:
            vm = next((item for item in snapshot.vms if item.id in vm_ids and item.cluster_id), None)
            if vm:
                cluster_id = vm.cluster_id
    if scope == "cluster" and not cluster_id:
        snapshot = request.app.state.worker.cached_snapshot()
        if snapshot and snapshot.clusters:
            cluster_id = snapshot.clusters[0].id
        else:
            raise HTTPException(status_code=400, detail="Pick a cluster, or use scope=global")
    adapter: InventoryAdapter = request.app.state.adapter
    try:
        payload = adapter.grant_migration_access(cluster_id=cluster_id, scope=scope)
    except Exception as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    job_id = (body.job_id or "").strip()
    if job_id:
        store: LocalStore = request.app.state.store
        job = store.requeue(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        request.app.state.worker.wake()
        payload["message"] = f"{payload.get('message', '')} Retrying job {job.title}."
    return MigrationAccessStatus.model_validate(payload)


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

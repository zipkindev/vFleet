from __future__ import annotations

import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

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
    ConnectionProfileList,
    ConsoleTicket,
    ConsoleTicketRequest,
    DatastoreListing,
    DeleteFileRequest,
    DiskConversionPlan,
    DiskConversionPlanRequest,
    DiskConversionRequest,
    DeployVmRequest,
    DrsOverrideRequest,
    CloneMigrateRequest,
    InventorySnapshot,
    Job,
    JobHistoryClearRequest,
    JobList,
    LoginRequest,
    LogoutRequest,
    ProfileDeleteRequest,
    MetricsResponse,
    MigrateVmRequest,
    GrantMigrationRequest,
    HostActionRequest,
    HostManagementInfo,
    HostServiceActionRequest,
    HostTimeRequest,
    MigrationAccessStatus,
    MkdirRequest,
    RenameVmRequest,
    StagingCreateRequest,
    StagingSession,
    UploadRequest,
    VmFolder,
)
from .reclaim import build_owner_reports
from .grouping import vm_matches_owner_query, vm_matches_search
from .metrics import build_owner_utilization
from .power import normalize_power_state
from .relay import RelayWorker
from .session import (
    apply_runtime,
    forget_vcenter,
    parse_endpoint,
    persist_vcenter,
    resolve_login_password,
    resolve_ssh_password,
)
from .store import LocalStore
from .profiles import ConnectionProfile, ConnectionProfileStore
from .vm_storage import normalize_disk_transform
from .errors import PermanentError

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


def _current_endpoint_fingerprint(request: Request) -> str:
    fingerprint = str(getattr(request.app.state, "current_endpoint_fingerprint", "") or "")
    if fingerprint:
        return fingerprint
    adapter: InventoryAdapter = request.app.state.adapter
    try:
        connection = adapter.connection()
        fingerprint = connection.endpoint_fingerprint
    except Exception:
        fingerprint = ""
    if not fingerprint:
        cached = request.app.state.store.load_inventory()
        fingerprint = cached.connection.endpoint_fingerprint if cached is not None else ""
    request.app.state.current_endpoint_fingerprint = fingerprint
    return fingerprint


def decorate_connection(conn: ConnectionInfo, request: Request) -> ConnectionInfo:
    worker: RelayWorker = request.app.state.worker
    decorated = worker.overlay(conn)
    active_profile_id = str(getattr(request.app.state, "active_profile_id", "") or "")
    if decorated.endpoint_fingerprint:
        request.app.state.current_endpoint_fingerprint = decorated.endpoint_fingerprint
        if active_profile_id:
            request.app.state.profile_store.mark_connected(
                active_profile_id, decorated.endpoint_kind, decorated.endpoint_fingerprint
            )
    return decorated.model_copy(
        update={
            "source": _source(request),
            "env_ready": settings.has_vcenter_creds,
            "has_saved_password": bool(settings.vcenter_password),
            "saved_host": settings.vcenter_host,
            "saved_user": settings.vcenter_user,
            "saved_port": settings.vcenter_port,
            "insecure": settings.vcenter_insecure,
            "ssh_user": settings.esxi_ssh_user,
            "ssh_port": settings.esxi_ssh_port,
            "ssh_host_key_sha256": settings.esxi_ssh_host_key_sha256,
            "has_saved_ssh_password": bool(settings.esxi_ssh_password),
            "active_profile_id": active_profile_id,
            "can_disconnect": conn.mode in {"vcenter", "esxi"},
        }
    )


def _filter_snapshot(snapshot: InventorySnapshot, q, owner, cluster, power, prefix) -> InventorySnapshot:
    vms = snapshot.vms
    if q:
        needle = q.lower()
        vms = [
            vm
            for vm in vms
            if vm_matches_search(vm.name, vm.owner_key, vm.deployed_by, vm.custom_fields, needle)
        ]
    if prefix:
        vms = [vm for vm in vms if vm.name.lower().startswith(prefix.lower())]
    if owner:
        vms = [
            vm
            for vm in vms
            if vm_matches_owner_query(vm.owner_key, vm.deployed_by, vm.custom_fields, owner)
        ]
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
    bound = dict(payload)
    adapter: InventoryAdapter = request.app.state.adapter
    connection = adapter.connection()
    fingerprint = connection.endpoint_fingerprint
    endpoint_kind = connection.endpoint_kind
    if not fingerprint:
        cached = store.load_inventory()
        if cached is not None:
            fingerprint = cached.connection.endpoint_fingerprint
            endpoint_kind = cached.connection.endpoint_kind
    if connection.mode != "demo" and not fingerprint:
        raise HTTPException(status_code=503, detail="Cannot bind the job to the current endpoint")
    bound["_endpoint_fingerprint"] = fingerprint
    bound["_endpoint_kind"] = endpoint_kind
    scoped_key = f"{fingerprint}:{idempotency_key}" if fingerprint and idempotency_key else idempotency_key
    job = store.enqueue(kind, title, bound, idempotency_key=scoped_key)
    worker.wake()
    return job


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = LocalStore(settings.data_dir / "vfleet.db")
    profile_store = ConnectionProfileStore(settings.data_dir / "connections.json")
    adapter = build_adapter(settings)
    app.state.store = store
    app.state.profile_store = profile_store
    app.state.adapter = adapter
    app.state.session_source = "env" if settings.resolved_mode in {"vcenter", "esxi"} else "demo"
    app.state.swap_lock = threading.Lock()
    app.state.active_profile_id = profile_store.import_settings(settings) if settings.has_vcenter_creds else ""
    app.state.current_endpoint_fingerprint = "demo" if settings.resolved_mode == "demo" else ""
    worker = RelayWorker(settings, store, lambda: app.state.adapter)
    app.state.worker = worker
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        _close(adapter)
        store.close()


APP_VERSION = "1.0.14"

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
        mode = adapter.connection().mode
    counts = request.app.state.store.counts() if hasattr(request.app.state, "store") else {}
    return {"ok": True, "mode": mode, "env_ready": settings.has_vcenter_creds, **counts}


@app.get("/api/version")
def version() -> dict:
    return {"version": APP_VERSION}


@app.get("/api/changelog")
def changelog() -> dict:
    changelog_path = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = "No changelog available."
    return {"content": text}


@app.get("/api/connection", response_model=ConnectionInfo, dependencies=[Depends(require_token)])
def connection(request: Request, adapter: InventoryAdapter = Depends(get_adapter)) -> ConnectionInfo:
    from .adapters.demo import DemoAdapter

    worker: RelayWorker = request.app.state.worker
    if isinstance(adapter, DemoAdapter):
        info = adapter.connection()
    else:
        live = adapter.connection()
        info = live.model_copy(
            update={
                "connected": worker.reachable is not False and not worker.last_error,
                "message": worker.last_error or live.message or "Local relay",
            }
        )
    return decorate_connection(info, request)


@app.get("/api/connection-profiles", response_model=ConnectionProfileList, dependencies=[Depends(require_token)])
def connection_profiles(request: Request) -> ConnectionProfileList:
    profile_store: ConnectionProfileStore = request.app.state.profile_store
    active_profile_id = str(getattr(request.app.state, "active_profile_id", "") or "")
    summaries = [profile.summary(active_profile_id) for profile in profile_store.list()]
    summaries.sort(key=lambda item: (not item.active, item.name.lower(), item.host.lower()))
    return ConnectionProfileList(profiles=summaries, active_profile_id=active_profile_id)


@app.post(
    "/api/connection-profiles/{profile_id}/connect",
    response_model=ConnectionInfo,
    dependencies=[Depends(require_token)],
)
def connect_profile(profile_id: str, request: Request) -> ConnectionInfo:
    profile = request.app.state.profile_store.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Unknown connection profile")
    return login(
        LoginRequest(
            host=profile.host,
            user=profile.user,
            password=profile.password,
            port=profile.port,
            insecure=profile.insecure,
            remember=True,
            connect=True,
            endpoint_kind=profile.endpoint_kind,
            ssh_enabled=profile.ssh_enabled,
            ssh_user=profile.ssh_user,
            ssh_password=profile.ssh_password,
            ssh_port=profile.ssh_port,
            ssh_host_key_sha256=profile.ssh_host_key_sha256,
            profile_id=profile.id,
            profile_name=profile.name,
        ),
        request,
    )


@app.delete("/api/connection-profiles/{profile_id}", dependencies=[Depends(require_token)])
def delete_connection_profile(profile_id: str, body: ProfileDeleteRequest, request: Request) -> dict:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Profile removal requires confirm=true")
    if profile_id == str(getattr(request.app.state, "active_profile_id", "") or ""):
        raise HTTPException(status_code=409, detail="Work offline or switch connections before removing the active profile")
    if not request.app.state.profile_store.delete(profile_id):
        raise HTTPException(status_code=404, detail="Unknown connection profile")
    return {"deleted": True, "profile_id": profile_id}


@app.post("/api/login", response_model=ConnectionInfo, dependencies=[Depends(require_token)])
def login(body: LoginRequest, request: Request) -> ConnectionInfo:
    host = body.host.strip()
    user = body.user.strip()
    if not host or not user:
        raise HTTPException(status_code=400, detail="Host, username, and password are required")
    try:
        endpoint, port = parse_endpoint(host, body.port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profile_store: ConnectionProfileStore = request.app.state.profile_store
    saved_profile = profile_store.get(body.profile_id) if body.profile_id else None
    if body.profile_id and saved_profile is None:
        raise HTTPException(status_code=404, detail="Unknown connection profile")
    matching_profile = bool(
        saved_profile
        and saved_profile.host.lower() == endpoint.lower()
        and saved_profile.user.lower() == user.lower()
        and saved_profile.port == port
    )
    password = body.password or (saved_profile.password if matching_profile and saved_profile else "")
    password = resolve_login_password(password, endpoint, user, port, settings)
    if not password:
        raise HTTPException(status_code=400, detail="Host, username, and password are required")
    ssh_user = body.ssh_user.strip()
    saved_ssh_password = ""
    if (
        matching_profile
        and saved_profile
        and saved_profile.ssh_user == ssh_user
        and saved_profile.ssh_port == body.ssh_port
    ):
        saved_ssh_password = saved_profile.ssh_password
    ssh_password = (
        resolve_ssh_password(
            body.ssh_password or saved_ssh_password, endpoint, ssh_user, body.ssh_port, settings
        )
        if body.ssh_enabled
        else ""
    )

    live = apply_runtime(
        settings,
        endpoint,
        user,
        password,
        port,
        body.insecure,
        ssh_enabled=body.ssh_enabled,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
        ssh_port=body.ssh_port,
        ssh_host_key_sha256=body.ssh_host_key_sha256,
    )
    candidate = VCenterAdapter(live)
    info = candidate.connection()
    if not info.connected:
        _close(candidate)
        detail = info.message or "Could not sign in to the vSphere endpoint"
        lowered = detail.lower()
        status = 401 if ("password" in lowered or "cannot complete login" in lowered or "incorrect user" in lowered) else 503
        raise HTTPException(status_code=status, detail=detail)

    if not body.connect:
        _close(candidate)
        info.message = f"Connection test passed. Detected {info.endpoint_kind.upper()}."
        counts = request.app.state.store.counts()
        return info.model_copy(
            update={
                "source": "test",
                "env_ready": settings.has_vcenter_creds,
                "has_saved_password": bool(settings.vcenter_password),
                "saved_host": settings.vcenter_host,
                "saved_user": settings.vcenter_user,
                "saved_port": settings.vcenter_port,
                "insecure": settings.vcenter_insecure,
                "ssh_user": settings.esxi_ssh_user,
                "ssh_port": settings.esxi_ssh_port,
                "ssh_host_key_sha256": settings.esxi_ssh_host_key_sha256,
                "has_saved_ssh_password": bool(settings.esxi_ssh_password),
                "queued_jobs": counts["queued"],
                "active_jobs": counts["active"],
            }
        )

    current = request.app.state.adapter.connection()
    current_fingerprint = current.endpoint_fingerprint or _current_endpoint_fingerprint(request)
    if current_fingerprint and current_fingerprint != info.endpoint_fingerprint:
        open_jobs = request.app.state.store.open_jobs()
        if open_jobs:
            _close(candidate)
            raise HTTPException(
                status_code=409,
                detail=f"Wait for or cancel {len(open_jobs)} queued/running job(s) before switching endpoints",
            )

    with request.app.state.swap_lock:
        previous = request.app.state.adapter
        request.app.state.adapter = candidate
        request.app.state.session_source = "ui"
        _close(previous)
        request.app.state.store.clear_runtime_cache()
        request.app.state.current_endpoint_fingerprint = info.endpoint_fingerprint

    settings.app_mode = info.endpoint_kind
    settings.vcenter_host = endpoint
    settings.vcenter_user = user
    settings.vcenter_password = password
    settings.vcenter_port = port
    settings.vcenter_insecure = body.insecure
    settings.esxi_ssh_enabled = body.ssh_enabled
    settings.esxi_ssh_user = ssh_user
    settings.esxi_ssh_password = ssh_password
    settings.esxi_ssh_port = body.ssh_port
    settings.esxi_ssh_host_key_sha256 = body.ssh_host_key_sha256
    if body.remember:
        persist_vcenter(
            settings,
            endpoint,
            user,
            password,
            port,
            body.insecure,
            endpoint_kind=info.endpoint_kind,
            ssh_enabled=body.ssh_enabled,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_port=body.ssh_port,
            ssh_host_key_sha256=body.ssh_host_key_sha256,
        )
        profile_id = saved_profile.id if saved_profile is not None else ""
        if not profile_id:
            existing = profile_store.find(endpoint, user, port)
            profile_id = existing.id if existing is not None else ""
        profile = ConnectionProfile(
            id=profile_id or str(uuid.uuid4()),
            name=body.profile_name.strip() or (saved_profile.name if saved_profile else "") or endpoint,
            host=endpoint,
            user=user,
            password=password,
            port=port,
            insecure=body.insecure,
            endpoint_kind=info.endpoint_kind,
            endpoint_fingerprint=info.endpoint_fingerprint,
            ssh_enabled=body.ssh_enabled,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_port=body.ssh_port,
            ssh_host_key_sha256=body.ssh_host_key_sha256,
            last_used_at=datetime.now(timezone.utc).isoformat(),
        )
        profile_store.save(profile)
        request.app.state.active_profile_id = profile.id
        info.message = f"{info.message}. Saved on this machine for the next start."
    else:
        request.app.state.active_profile_id = ""
        info.message = f"{info.message}. Connected for this server session only."
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
        request.app.state.store.clear_runtime_cache()
    if payload.forget:
        active_profile_id = str(getattr(request.app.state, "active_profile_id", "") or "")
        if active_profile_id:
            request.app.state.profile_store.delete(active_profile_id)
        forget_vcenter(settings)
    request.app.state.active_profile_id = ""
    request.app.state.current_endpoint_fingerprint = "demo"
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
    hours: float = Query(default=24.0, ge=0.25, le=336.0),
    owner: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
    datastore: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
) -> MetricsResponse:
    store: LocalStore = request.app.state.store
    owner_key = (owner or "").strip()
    host_id = (host or "").strip()
    datastore_id = (datastore or "").strip()
    series_owner = "" if host_id else owner_key
    series = store.load_metrics(
        hours=hours,
        owner=series_owner,
        host_id=host_id,
        datastore_id=datastore_id,
        since=since,
        until=until,
    )
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
        host=host_id,
        datastore=datastore_id,
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


@app.get("/api/folders", response_model=List[VmFolder], dependencies=[Depends(require_token)])
def list_folders(
    adapter: InventoryAdapter = Depends(get_adapter),
    datacenter: str = Query(default=""),
) -> List[VmFolder]:
    try:
        return adapter.list_vm_folders(datacenter=datacenter.strip())
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
    return _enqueue_template_deploy(request, body, kind="clone", title_prefix="Clone")


@app.post("/api/vms/deploy", response_model=Job, dependencies=[Depends(require_token)])
def deploy_vm(body: DeployVmRequest, request: Request) -> Job:
    return _enqueue_template_deploy(request, body, kind="deploy", title_prefix="Deploy")


def _enqueue_template_deploy(
    request: Request,
    body: CloneVmRequest,
    *,
    kind: str,
    title_prefix: str,
) -> Job:
    name = body.name.strip()
    if not name or "/" in name:
        raise HTTPException(status_code=400, detail="VM name is required and cannot contain '/'")
    if not body.template_id or not body.datastore_id:
        raise HTTPException(status_code=400, detail="Template and datastore are required")
    if body.cpu_count is not None and not 1 <= body.cpu_count <= 128:
        raise HTTPException(status_code=400, detail="cpu_count must be between 1 and 128")
    if body.memory_mib is not None and not 128 <= body.memory_mib <= 1_048_576:
        raise HTTPException(status_code=400, detail="memory_mib is out of range")
    if body.disable_drs and not (body.host_id or "").strip():
        raise HTTPException(status_code=400, detail="disable_drs requires a destination host")
    return _enqueue(
        request,
        kind,
        f"{title_prefix} {name}",
        body.model_dump(),
        idempotency_key=f"{kind}:{name}",
    )


@app.post("/api/vms/{vm_id}/console", response_model=ConsoleTicket, dependencies=[Depends(require_token)])
def vm_console(
    vm_id: str,
    request: Request,
    adapter: InventoryAdapter = Depends(get_adapter),
    body: Optional[ConsoleTicketRequest] = None,
) -> ConsoleTicket:
    ticket_type = (body.type if body else "vmrc").strip().lower()
    if ticket_type not in {"vmrc", "webmks"}:
        raise HTTPException(status_code=400, detail="type must be vmrc or webmks")
    try:
        return adapter.console_ticket(vm_id.strip(), ticket_type)
    except PermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vms/{vm_id}/rename", response_model=Job, dependencies=[Depends(require_token)])
def rename_vm(vm_id: str, body: RenameVmRequest, request: Request) -> Job:
    target_id = vm_id.strip()
    name = body.name.strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="vm_id is required")
    if not name or "/" in name:
        raise HTTPException(status_code=400, detail="VM name is required and cannot contain '/'")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to rename")
    snapshot = request.app.state.worker.cached_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Inventory is not ready yet")
    source = next((vm for vm in snapshot.vms if vm.id == target_id), None)
    if source is None:
        raise HTTPException(status_code=400, detail="Unknown VM")
    if name == source.name:
        raise HTTPException(status_code=400, detail="New name is the same as the current name")
    if any(vm.name == name and vm.id != target_id for vm in snapshot.vms):
        raise HTTPException(status_code=400, detail=f"VM name already exists: {name}")
    return _enqueue(
        request,
        "rename",
        f"Rename {source.name} → {name}",
        {"vm_id": target_id, "name": name, "confirm": True},
        idempotency_key=f"rename:{target_id}:{name}",
    )


@app.post("/api/vms/drs-override", response_model=Job, dependencies=[Depends(require_token)])
def apply_drs_override(body: DrsOverrideRequest, request: Request) -> Job:
    vm_ids = [item.strip() for item in body.vm_ids if item and item.strip()]
    if not vm_ids:
        raise HTTPException(status_code=400, detail="No virtual machines selected")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to apply a DRS override")
    if len(vm_ids) > 50:
        raise HTTPException(
            status_code=400,
            detail="Refusing more than 50 VMs in one request (local relay batch limit; split into a second job)",
        )
    snapshot = request.app.state.worker.cached_snapshot()
    names: List[str] = []
    if snapshot is not None:
        by_id = {vm.id: vm.name for vm in snapshot.vms}
        names = [by_id.get(vm_id, vm_id) for vm_id in vm_ids]
    title = f"DRS override {names[0]}" if len(names) == 1 else f"DRS override {len(vm_ids)} VM(s)"
    return _enqueue(
        request,
        "drs_override",
        title,
        body.model_dump(),
        idempotency_key="drs_override:" + ",".join(sorted(vm_ids)),
    )


@app.post("/api/vms/clone-migrate", response_model=Job, dependencies=[Depends(require_token)])
def clone_migrate_vm(body: CloneMigrateRequest, request: Request) -> Job:
    vm_id = (body.vm_id or "").strip()
    host_id = (body.host_id or "").strip()
    if not vm_id:
        raise HTTPException(status_code=400, detail="vm_id is required")
    if not host_id:
        raise HTTPException(status_code=400, detail="Destination host is required")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to clone-migrate")
    snapshot = request.app.state.worker.cached_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Inventory is not ready yet")
    source = next((vm for vm in snapshot.vms if vm.id == vm_id), None)
    if source is None:
        raise HTTPException(status_code=400, detail="Unknown VM")
    host = next((item for item in snapshot.hosts if item.id == host_id), None)
    if host is None:
        raise HTTPException(status_code=400, detail="Unknown host")
    if source.cluster_id and host.cluster_id and source.cluster_id != host.cluster_id:
        raise HTTPException(status_code=400, detail=f"{source.name} is not in the same cluster as {host.name}")

    destroy = bool(body.destroy_source)
    original_name = source.name
    if destroy:
        short = vm_id.replace("vm-", "")[-6:] or "tmp"
        clone_name = f"{original_name}-migrate-{short}"
        if any(vm.name == clone_name for vm in snapshot.vms):
            clone_name = f"{original_name}-migrate-{short}-2"
        title = f"Replace {original_name} → {host.name}"
    else:
        clone_name = (body.name or "").strip() or f"{original_name}-clone"
        if not clone_name or "/" in clone_name:
            raise HTTPException(status_code=400, detail="Clone name is required and cannot contain '/'")
        if any(vm.name == clone_name for vm in snapshot.vms):
            raise HTTPException(status_code=400, detail=f"VM name already exists: {clone_name}")
        title = f"Clone {original_name} → {host.name}"

    payload = body.model_copy(
        update={
            "vm_id": vm_id,
            "host_id": host_id,
            "name": clone_name,
            "datastore_id": (body.datastore_id or "").strip(),
            "destroy_source": destroy,
        }
    ).model_dump()
    payload["original_name"] = original_name
    return _enqueue(
        request,
        "clone_migrate",
        title,
        payload,
        idempotency_key=f"clone_migrate:{vm_id}:{host_id}:{clone_name}:{int(destroy)}",
    )


@app.post("/api/vms/disk-conversion/plan", response_model=DiskConversionPlan, dependencies=[Depends(require_token)])
def plan_disk_conversion(
    body: DiskConversionPlanRequest,
    adapter: InventoryAdapter = Depends(get_adapter),
) -> DiskConversionPlan:
    try:
        return adapter.disk_conversion_plan(body)
    except PermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vms/disk-conversion", response_model=Job, dependencies=[Depends(require_token)])
def convert_vm_disks(body: DiskConversionRequest, request: Request) -> Job:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Review the plan and set confirm=true to convert disks")
    adapter: InventoryAdapter = request.app.state.adapter
    try:
        plan = adapter.disk_conversion_plan(
            DiskConversionPlanRequest(vm_id=body.vm_id, target=body.target, method=body.method)
        )
    except PermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if plan.plan_token != body.plan_token:
        raise HTTPException(status_code=409, detail="The VM changed after the plan was created; review a fresh plan")
    if plan.noop:
        raise HTTPException(status_code=400, detail="All supported disks already use that provisioning type")
    if not plan.can_execute:
        raise HTTPException(status_code=400, detail="; ".join(plan.blockers) or "Conversion cannot be executed")
    return _enqueue(
        request,
        "disk_convert",
        f"Convert {plan.vm_name} disks to {plan.target.replace('_', ' ')}",
        {"plan": plan.model_dump(mode="json")},
        idempotency_key=f"disk_convert:{plan.plan_token}",
    )


@app.get("/api/host", response_model=HostManagementInfo, dependencies=[Depends(require_token)])
def host_management(adapter: InventoryAdapter = Depends(get_adapter)) -> HostManagementInfo:
    try:
        return adapter.host_management()
    except PermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/host/actions", response_model=Job, dependencies=[Depends(require_token)])
def host_action(body: HostActionRequest, request: Request) -> Job:
    action = body.action.strip().lower()
    if action not in {"maintenance_enter", "maintenance_exit", "reboot", "shutdown"}:
        raise HTTPException(status_code=400, detail="Unknown host action")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Review host state and set confirm=true")
    return _enqueue(
        request,
        "host_action",
        f"Host {action.replace('_', ' ')}",
        body.model_dump(),
        idempotency_key=f"host_action:{action}",
    )


@app.post("/api/host/services", response_model=Job, dependencies=[Depends(require_token)])
def host_service(body: HostServiceActionRequest, request: Request) -> Job:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to change a host service")
    return _enqueue(
        request,
        "host_service",
        f"{body.action.title()} host service {body.service_key}",
        body.model_dump(),
        idempotency_key=f"host_service:{body.service_key}:{body.action}:{body.policy}",
    )


@app.post("/api/host/time", response_model=Job, dependencies=[Depends(require_token)])
def host_time(body: HostTimeRequest, request: Request) -> Job:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to update host time settings")
    return _enqueue(
        request,
        "host_time",
        "Update host NTP settings",
        body.model_dump(),
        idempotency_key="host_time:" + ",".join(sorted(body.ntp_servers)),
    )


@app.post("/api/host/storage/rescan", response_model=Job, dependencies=[Depends(require_token)])
def host_storage_rescan(body: HostActionRequest, request: Request) -> Job:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to rescan host storage")
    return _enqueue(request, "host_storage_rescan", "Rescan host storage", {}, idempotency_key="host_storage_rescan")


@app.post("/api/host/support-bundle", response_model=Job, dependencies=[Depends(require_token)])
def host_support_bundle(body: HostActionRequest, request: Request) -> Job:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to generate a support bundle")
    return _enqueue(request, "host_support_bundle", "Generate ESXi support bundle", {}, idempotency_key="host_support_bundle")


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
    folder_id = (body.folder_id or "").strip()
    if not host_id and not datastore_id and not network_id and not disk and not folder_id:
        raise HTTPException(
            status_code=400,
            detail="Pick a destination host, datastore, network, disk type, or folder",
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
    if folder_id:
        parts.append("folder")
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
            "folder_id": folder_id,
        }
    )
    return _enqueue(
        request,
        "migrate",
        "Migrate " + " ".join(parts),
        payload,
        idempotency_key="migrate:"
        + ",".join(sorted(vm_ids))
        + f":{host_id}:{datastore_id}:{network_id}:{disk}:{folder_id}",
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
def jobs(request: Request, store: LocalStore = Depends(get_store)) -> JobList:
    fingerprint = _current_endpoint_fingerprint(request)
    rows = store.list_jobs(endpoint_fingerprint=fingerprint)
    counts = store.counts(endpoint_fingerprint=fingerprint)
    hidden = max(0, store.job_count() - store.job_count(fingerprint))
    return JobList(
        jobs=rows,
        queued=counts["queued"],
        active=counts["active"],
        endpoint_fingerprint=fingerprint,
        hidden_other_endpoints=hidden,
    )


@app.delete("/api/jobs/history", dependencies=[Depends(require_token)])
def clear_job_history(body: JobHistoryClearRequest, request: Request) -> dict:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Clearing job history requires confirm=true")
    fingerprint = _current_endpoint_fingerprint(request)
    if not fingerprint:
        raise HTTPException(status_code=409, detail="No active endpoint is available for scoped cleanup")
    if body.other_endpoints:
        deleted = request.app.state.store.clear_other_job_history(fingerprint)
    else:
        deleted = request.app.state.store.clear_job_history(fingerprint)
    return {"deleted": deleted, "endpoint_fingerprint": "other" if body.other_endpoints else fingerprint}


@app.get("/api/jobs/{job_id}", response_model=Job, dependencies=[Depends(require_token)])
def job_detail(job_id: str, request: Request, store: LocalStore = Depends(get_store)) -> Job:
    job = store.get(job_id)
    if job is None or str(job.payload.get("_endpoint_fingerprint") or "") != _current_endpoint_fingerprint(request):
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/api/jobs/{job_id}/cancel", response_model=Job, dependencies=[Depends(require_token)])
def cancel_job(job_id: str, request: Request, store: LocalStore = Depends(get_store)) -> Job:
    existing = store.get(job_id)
    if existing is None or str(existing.payload.get("_endpoint_fingerprint") or "") != _current_endpoint_fingerprint(request):
        raise HTTPException(status_code=404, detail="Unknown job")
    job = store.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/api/jobs/{job_id}/retry", response_model=Job, dependencies=[Depends(require_token)])
def retry_job(job_id: str, request: Request) -> Job:
    store: LocalStore = request.app.state.store
    existing = store.get(job_id)
    if existing is None or str(existing.payload.get("_endpoint_fingerprint") or "") != _current_endpoint_fingerprint(request):
        raise HTTPException(status_code=404, detail="Unknown job")
    job = store.requeue(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    request.app.state.worker.wake()
    return job


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def delete_job(job_id: str, request: Request) -> dict:
    store: LocalStore = request.app.state.store
    existing = store.get(job_id)
    fingerprint = _current_endpoint_fingerprint(request)
    if existing is None or str(existing.payload.get("_endpoint_fingerprint") or "") != fingerprint:
        raise HTTPException(status_code=404, detail="Unknown job")
    if existing.status not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only completed, failed, or cancelled jobs can be removed")
    if not store.delete_terminal_job(job_id, fingerprint):
        raise HTTPException(status_code=404, detail="Unknown job")
    return {"deleted": True, "job_id": job_id}


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

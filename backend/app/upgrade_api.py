from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request

from .errors import PermanentError
from .host_upgrade import (UpgradeInspectRequest, UpgradePrepareRequest, UpgradeRecoverRequest,
                           load_run, media_path, validate_prepare)
from .usb_media import UsbWriteRequest, list_usb_devices, validate_usb_write


def install_upgrade_routes(app, require_token, enqueue, current_fingerprint):
    def run_for(request: Request, ident: str) -> dict:
        try:
            run = load_run(request.app.state.store, ident)
        except (ValueError, PermanentError) as exc:
            raise HTTPException(404, "Unknown upgrade plan") from exc
        if run["endpoint_fingerprint"] != current_fingerprint(request):
            raise HTTPException(404, "Unknown upgrade plan for this endpoint")
        run["reserved"] = request.app.state.store.upgrade_lock(run["endpoint_fingerprint"]) == run["id"]
        return run

    def queue(request, kind, title, payload, key):
        try:
            return enqueue(request, kind, title, payload, key)
        except PermanentError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/host/upgrade/inspect", dependencies=[Depends(require_token)])
    def inspect(body: UpgradeInspectRequest, request: Request):
        try:
            media_path(request.app.state.store, str(body.staging_id))
        except (ValueError, PermanentError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return queue(request, "host_upgrade_inspect", "Inspect ESXi ISO and host readiness (no shutdown)",
                     body.model_dump(mode="json"), "host_upgrade_inspect:" + str(body.staging_id))

    @app.get("/api/host/upgrade/active", dependencies=[Depends(require_token)])
    def active(request: Request):
        fingerprint = current_fingerprint(request)
        store = request.app.state.store
        ident = store.upgrade_lock(fingerprint) or store.latest_upgrade_plan(fingerprint)
        return {"plan": run_for(request, ident) if ident else None}

    @app.get("/api/host/upgrade/plans/{plan_id}", dependencies=[Depends(require_token)])
    def get_plan(plan_id: UUID, request: Request):
        return run_for(request, str(plan_id))

    @app.get("/api/host/upgrade/usb", dependencies=[Depends(require_token)])
    def usb_devices():
        return list_usb_devices()

    @app.post("/api/host/upgrade/usb/write", dependencies=[Depends(require_token)])
    def usb_write(body: UsbWriteRequest, request: Request):
        run = run_for(request, str(body.plan_id))
        try:
            checked, _path, device = validate_usb_write(request.app.state.store, body)
            if checked["id"] != run["id"]:
                raise PermanentError("USB request does not match this upgrade plan")
        except PermanentError as exc:
            raise HTTPException(400, str(exc)) from exc
        return queue(
            request,
            "host_upgrade_usb_write",
            f"Create verified ESXi installer USB on Disk {device['number']}",
            body.model_dump(mode="json"),
            f"host_upgrade_usb_write:{run['id']}:{device['number']}:{run['media']['sha256']}",
        )

    @app.post("/api/host/upgrade/prepare", dependencies=[Depends(require_token)])
    def prepare(body: UpgradePrepareRequest, request: Request):
        run = run_for(request, str(body.plan_id))
        try:
            validate_prepare(run, body)
            if run["phase"] not in {"planned", "backing_up", "shutting_down", "entering_maintenance"}:
                raise PermanentError("This upgrade cannot be prepared in its current phase")
        except PermanentError as exc:
            raise HTTPException(400, str(exc)) from exc
        return queue(request, "host_upgrade_prepare", "Prepare ESXi host for manual ISO upgrade",
                     body.model_dump(mode="json"), "host_upgrade_prepare:" + str(body.plan_id))

    @app.post("/api/host/upgrade/recover", dependencies=[Depends(require_token)])
    def recover(body: UpgradeRecoverRequest, request: Request):
        run_for(request, str(body.plan_id))
        if not body.confirm:
            raise HTTPException(400, "Confirm host verification and VM restoration")
        return queue(request, "host_upgrade_recover", "Verify ESXi host and restore VM power states",
                     body.model_dump(mode="json"), "host_upgrade_recover:" + str(body.plan_id) + ":" + body.mode)

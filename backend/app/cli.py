from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any, Dict

import httpx

from .config import Settings


class Client:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {"X-UI-Token": token} if token else {}

    def request(self, method: str, path: str, body: Dict[str, Any] | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.url}{path}",
                headers=self.headers,
                json=body,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise SystemExit(f"vFleet API is unavailable: {exc}") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise SystemExit(f"HTTP {response.status_code}: {detail}")
        return response.json()


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vfleet", description="Local vFleet API and ESXi automation CLI")
    root.add_argument("--url", default=os.getenv("VFLEET_API_URL", "http://127.0.0.1:8080"))
    root.add_argument("--token", default=os.getenv("VFLEET_UI_TOKEN", Settings().ui_token))
    commands = root.add_subparsers(dest="command", required=True)

    connect = commands.add_parser("connect", help="Test or save a vCenter/ESXi connection")
    connect.add_argument("host")
    connect.add_argument("--user", required=True)
    connect.add_argument("--port", type=int, default=443)
    connect.add_argument("--verify-tls", action="store_true")
    connect.add_argument("--test", action="store_true", help="Test only; do not switch or save")
    connect.add_argument("--session-only", action="store_true", help="Switch without writing .env")
    connect.add_argument("--ssh-user", default="")
    connect.add_argument("--ssh-port", type=int, default=22)
    connect.add_argument("--ssh-host-key", default="")

    commands.add_parser("connection", help="Show endpoint detection and capabilities")
    inventory = commands.add_parser("inventory", help="List inventory")
    inventory.add_argument("--kind", choices=["summary", "vms", "hosts"], default="summary")

    commands.add_parser("host", help="Show standalone ESXi administration data")
    action = commands.add_parser("host-action", help="Queue a host lifecycle action")
    action.add_argument("action", choices=["maintenance_enter", "maintenance_exit", "reboot", "shutdown"])
    action.add_argument("--yes", action="store_true")
    service = commands.add_parser("service", help="Control an allowlisted ESXi service")
    service.add_argument("key", choices=["TSM", "TSM-SSH", "ntpd"])
    service.add_argument("action", choices=["start", "stop", "restart", "policy"])
    service.add_argument("--policy", choices=["on", "off", "automatic"], default="")
    service.add_argument("--yes", action="store_true")
    ntp = commands.add_parser("ntp", help="Configure ESXi NTP")
    ntp.add_argument("servers", nargs="*")
    ntp.add_argument("--sync", action="store_true")
    ntp.add_argument("--yes", action="store_true")
    for name, help_text in (("rescan", "Queue host storage rescan"), ("support-bundle", "Queue support bundle")):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--yes", action="store_true")

    disk_plan = commands.add_parser("disk-plan", help="Review VM disk conversion safety")
    disk_plan.add_argument("vm_id")
    disk_plan.add_argument("--target", choices=["thin", "lazy_zeroed_thick", "eager_zeroed_thick"], default="thin")
    disk_plan.add_argument("--method", choices=["auto", "soap", "ssh"], default="auto")
    disk_convert = commands.add_parser("disk-convert", help="Plan and queue disk provisioning conversion")
    disk_convert.add_argument("vm_id")
    disk_convert.add_argument("--target", choices=["thin", "lazy_zeroed_thick", "eager_zeroed_thick"], default="thin")
    disk_convert.add_argument("--method", choices=["auto", "soap", "ssh"], default="auto")
    disk_convert.add_argument("--yes", action="store_true")

    jobs = commands.add_parser("jobs", help="List persistent jobs")
    jobs.add_argument("--limit", type=int, default=80)
    job = commands.add_parser("job", help="Show or wait for one job")
    job.add_argument("job_id")
    job.add_argument("--wait", action="store_true")
    job.add_argument("--timeout", type=int, default=3600)
    return root


def confirmed(flag: bool, prompt: str) -> bool:
    if flag:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("Refusing an infrastructure-changing operation without --yes")
    return input(f"{prompt} Type yes to continue: ").strip().lower() == "yes"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    api = Client(args.url, args.token)
    command = args.command
    if command == "connect":
        password = getpass.getpass("vSphere password: ")
        ssh_enabled = bool(args.ssh_user or args.ssh_host_key)
        ssh_password = getpass.getpass("ESXi SSH password (blank for key/agent): ") if ssh_enabled else ""
        emit(api.request("POST", "/api/login", {
            "host": args.host, "user": args.user, "password": password, "port": args.port,
            "insecure": not args.verify_tls, "connect": not args.test,
            "remember": not args.test and not args.session_only, "endpoint_kind": "auto",
            "ssh_enabled": ssh_enabled, "ssh_user": args.ssh_user, "ssh_password": ssh_password,
            "ssh_port": args.ssh_port, "ssh_host_key_sha256": args.ssh_host_key,
        }))
    elif command == "connection":
        emit(api.request("GET", "/api/connection"))
    elif command == "inventory":
        payload = api.request("GET", "/api/inventory")
        if args.kind == "vms": emit(payload["vms"])
        elif args.kind == "hosts": emit(payload["hosts"])
        else: emit({"connection": payload["connection"], "clusters": len(payload["clusters"]), "hosts": len(payload["hosts"]), "vms": len(payload["vms"])})
    elif command == "host":
        emit(api.request("GET", "/api/host"))
    elif command == "host-action":
        if confirmed(args.yes, f"Queue host action {args.action}?"):
            emit(api.request("POST", "/api/host/actions", {"action": args.action, "confirm": True}))
    elif command == "service":
        if confirmed(args.yes, f"{args.action} service {args.key}?"):
            emit(api.request("POST", "/api/host/services", {"service_key": args.key, "action": args.action, "policy": args.policy, "confirm": True}))
    elif command == "ntp":
        if confirmed(args.yes, "Update host NTP configuration?"):
            emit(api.request("POST", "/api/host/time", {"ntp_servers": args.servers, "sync_now": args.sync, "confirm": True}))
    elif command in {"rescan", "support-bundle"}:
        if confirmed(args.yes, f"Queue {command}?"):
            path = "/api/host/storage/rescan" if command == "rescan" else "/api/host/support-bundle"
            emit(api.request("POST", path, {"action": command, "confirm": True}))
    elif command in {"disk-plan", "disk-convert"}:
        plan = api.request("POST", "/api/vms/disk-conversion/plan", {"vm_id": args.vm_id, "target": args.target, "method": args.method})
        if command == "disk-plan": emit(plan)
        elif not plan["can_execute"]:
            emit(plan); raise SystemExit("Plan is not executable")
        elif confirmed(args.yes, f"Convert {plan['vm_name']} disks to {plan['target']} using {plan['method']}?"):
            emit(api.request("POST", "/api/vms/disk-conversion", {"vm_id": plan["vm_id"], "target": plan["target"], "method": plan["method"], "plan_token": plan["plan_token"], "confirm": True}))
    elif command == "jobs":
        emit(api.request("GET", "/api/jobs"))
    elif command == "job":
        deadline = time.monotonic() + args.timeout
        while True:
            job = api.request("GET", f"/api/jobs/{args.job_id}")
            if not args.wait or job["status"] in {"complete", "failed", "cancelled"}:
                emit(job)
                return 1 if job["status"] == "failed" else 0
            if time.monotonic() >= deadline:
                raise SystemExit("Timed out waiting for job")
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

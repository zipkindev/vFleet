from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any, Dict
from urllib.parse import urlencode

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
    connect.add_argument("--jump-address", default="")
    connect.add_argument("--jump-port", type=int, default=22)
    connect.add_argument("--jump-host-type", choices=["auto", "windows", "unix"], default="auto")
    connect.add_argument("--jump-credential-id", default="")

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

    for name, help_text in (
        ("hardware-plan", "Review one shared CPU, memory, disk, or ISO change for multiple VMs"),
        ("hardware-config", "Plan and queue one shared hardware configuration for multiple VMs"),
    ):
        hardware = commands.add_parser(name, help=help_text)
        hardware.add_argument("vm_ids", nargs="+")
        hardware.add_argument("--cpu", type=int)
        hardware.add_argument("--memory-gib", type=float)
        hardware.add_argument("--disk", type=int, help="One-based disk position")
        hardware.add_argument("--disk-gib", type=float, help="Target disk size (not an increment)")
        hardware.add_argument("--iso-datastore", default="")
        hardware.add_argument("--iso-path", default="")
        hardware.add_argument("--eject-iso", action="store_true")
        hardware.add_argument("--no-connect-at-power-on", action="store_true")
        hardware.add_argument("--shutdown-before", action="store_true", help="Request guest shutdown before applying")
        hardware.add_argument(
            "--force-power-off",
            action="store_true",
            help="Force power off if guest shutdown does not finish within two minutes",
        )
        hardware.add_argument("--power-on-after", action="store_true", help="Restore VMs that were originally powered on")
        if name == "hardware-config":
            hardware.add_argument("--yes", action="store_true")

    commands.add_parser("credentials", help="List Automation Vault credential metadata (never secrets)")
    credential_add = commands.add_parser("credential-add", help="Add a reusable encrypted automation credential")
    credential_add.add_argument("name")
    credential_add.add_argument("--kind", choices=["windows", "ssh", "service"], required=True)
    credential_add.add_argument("--user", default="")
    credential_add.add_argument("--scope", choices=["endpoint", "global"], default="endpoint")
    credential_delete = commands.add_parser("credential-delete", help="Delete an Automation Vault credential")
    credential_delete.add_argument("credential_id")
    credential_delete.add_argument("--yes", action="store_true")
    tools = commands.add_parser("tools-deploy", help="Queue bulk Windows/Linux/pfSense VMware Tools deployment")
    tools.add_argument("credential_id")
    tools.add_argument("targets", nargs="+", metavar="VM_ID=ADDRESS")
    tools.add_argument("--os", choices=["auto", "windows", "linux", "pfsense"], default="auto")
    tools.add_argument("--windows-https", action="store_true")
    tools.add_argument("--windows-port", type=int, default=0)
    tools.add_argument("--no-verify-tls", action="store_true")
    tools.add_argument("--ssh-port", type=int, default=22)
    tools.add_argument("--no-sudo", action="store_true")
    tools.add_argument("--jump-address", default="")
    tools.add_argument("--jump-port", type=int, default=22)
    tools.add_argument("--jump-host-type", choices=["auto", "windows", "unix"], default="auto")
    tools.add_argument("--jump-credential-id", default="")
    tools.add_argument("--no-jump", action="store_true", help="Do not inherit the active connection's jump host")
    tools.add_argument("--yes", action="store_true")

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
        jump_host_key = ""
        if args.jump_address:
            if not args.jump_credential_id:
                raise SystemExit("--jump-credential-id is required with --jump-address")
            jump_query = urlencode({"address": args.jump_address, "port": args.jump_port})
            jump_host_key = api.request("GET", f"/api/tools/ssh-host-key?{jump_query}")["fingerprint"]
            api.request("POST", "/api/tools/ssh-connection-test", {
                "address": args.jump_address,
                "port": args.jump_port,
                "credential_id": args.jump_credential_id,
                "host_key_sha256": jump_host_key,
                "host_type": args.jump_host_type,
            })
        emit(api.request("POST", "/api/login", {
            "host": args.host, "user": args.user, "password": password, "port": args.port,
            "insecure": not args.verify_tls, "connect": not args.test,
            "remember": not args.test and not args.session_only, "endpoint_kind": "auto",
            "ssh_enabled": ssh_enabled, "ssh_user": args.ssh_user, "ssh_password": ssh_password,
            "ssh_port": args.ssh_port, "ssh_host_key_sha256": args.ssh_host_key,
            "jump_enabled": bool(args.jump_address), "jump_address": args.jump_address,
            "jump_port": args.jump_port, "jump_host_type": args.jump_host_type,
            "jump_credential_id": args.jump_credential_id,
            "jump_host_key_sha256": jump_host_key,
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
    elif command in {"hardware-plan", "hardware-config"}:
        if (args.disk is None) != (args.disk_gib is None):
            raise SystemExit("Use --disk and --disk-gib together")
        if args.eject_iso and (args.iso_datastore or args.iso_path):
            raise SystemExit("Use either --eject-iso or --iso-datastore/--iso-path")
        body = {
            "vm_ids": args.vm_ids,
            "cpu_count": args.cpu,
            "memory_mib": round(args.memory_gib * 1024) if args.memory_gib is not None else None,
            "disk_index": args.disk - 1 if args.disk is not None else None,
            "disk_capacity_bytes": round(args.disk_gib * 1024**3) if args.disk_gib is not None else None,
            "iso_action": "eject" if args.eject_iso else ("mount" if args.iso_datastore or args.iso_path else "keep"),
            "iso_datastore_id": args.iso_datastore,
            "iso_path": args.iso_path,
            "iso_connect_at_power_on": not args.no_connect_at_power_on,
            "shutdown_before": args.shutdown_before,
            "force_power_off_on_timeout": args.force_power_off,
            "power_on_after": args.power_on_after,
            "shutdown_timeout_seconds": 120,
        }
        plan = api.request("POST", "/api/vms/hardware/plan", body)
        if command == "hardware-plan":
            emit(plan)
        elif not plan["can_execute_count"]:
            emit(plan); raise SystemExit("No VM has an eligible hardware change")
        elif confirmed(args.yes, f"Queue the reviewed hardware change for {plan['can_execute_count']} VM(s)?"):
            emit(api.request("POST", "/api/vms/hardware", {**body, "plan_token": plan["plan_token"], "confirm": True}))
    elif command == "credentials":
        emit(api.request("GET", "/api/automation-credentials"))
    elif command == "credential-add":
        secret = getpass.getpass("Password or token: ")
        if not secret:
            raise SystemExit("A non-empty password or token is required")
        emit(api.request("POST", "/api/automation-credentials", {
            "name": args.name, "kind": args.kind, "username": args.user,
            "secret": secret, "scope": args.scope, "confirm": True,
        }))
    elif command == "credential-delete":
        if confirmed(args.yes, f"Delete Automation Vault credential {args.credential_id}?"):
            emit(api.request("DELETE", f"/api/automation-credentials/{args.credential_id}", {"confirm": True}))
    elif command == "tools-deploy":
        inventory = api.request("GET", "/api/inventory")
        by_id = {item["id"]: item for item in inventory["vms"]}
        targets = []
        jump_address = args.jump_address
        jump_port = args.jump_port
        jump_host_type = args.jump_host_type
        jump_credential_id = args.jump_credential_id
        if not args.no_jump and not jump_address:
            profiles = api.request("GET", "/api/connection-profiles")
            active = next((item for item in profiles["profiles"] if item["id"] == profiles["active_profile_id"]), None)
            if active and active.get("jump_enabled"):
                jump_address = active.get("jump_address", "")
                jump_port = int(active.get("jump_port", 22))
                jump_host_type = str(active.get("jump_host_type", "auto"))
                jump_credential_id = active.get("jump_credential_id", "")
        jump_host_key = ""
        if jump_address:
            if not jump_credential_id:
                raise SystemExit("--jump-credential-id is required with --jump-address")
            jump_query = urlencode({"address": jump_address, "port": jump_port})
            jump_host_key = api.request("GET", f"/api/tools/ssh-host-key?{jump_query}")["fingerprint"]
        for raw in args.targets:
            if "=" not in raw:
                raise SystemExit(f"Invalid target {raw!r}; use VM_ID=ADDRESS")
            vm_id, address = (part.strip() for part in raw.split("=", 1))
            vm = by_id.get(vm_id)
            if vm is None or not address:
                raise SystemExit(f"Unknown VM or blank address: {raw}")
            family = args.os
            if family == "auto":
                guest = str(vm.get("guest_os") or "").lower()
                name = str(vm.get("name") or "").lower()
                family = "windows" if "win" in guest else "pfsense" if (
                    "pfsense" in guest or "pfsense" in name or ("freebsd" in guest and any(token in name for token in ("router", "firewall")))
                ) else "linux" if any(
                    token in guest for token in ("linux", "ubuntu", "debian", "rhel", "centos", "suse", "oracle", "photon", "fedora")
                ) else "auto"
            target = {"vm_id": vm_id, "address": address, "os_family": family}
            if family in {"linux", "pfsense"}:
                query_values = {"address": address, "port": args.ssh_port}
                if jump_address:
                    query_values.update({
                        "jump_address": jump_address,
                        "jump_port": jump_port,
                        "jump_credential_id": jump_credential_id,
                        "jump_host_key_sha256": jump_host_key,
                    })
                query = urlencode(query_values)
                host_key = api.request("GET", f"/api/tools/ssh-host-key?{query}")
                target["ssh_host_key_sha256"] = host_key["fingerprint"]
            targets.append(target)
        preview = ", ".join(f"{by_id[item['vm_id']]['name']} ({item['os_family']})" for item in targets)
        if confirmed(args.yes, f"Queue VMware Tools deployment for {preview}?"):
            emit(api.request("POST", "/api/tools/deploy", {
                "targets": targets, "credential_id": args.credential_id,
                "windows_transport": "https" if args.windows_https else "http",
                "windows_port": args.windows_port or (5986 if args.windows_https else 5985),
                "validate_certificate": not args.no_verify_tls,
                "linux_port": args.ssh_port, "sudo": not args.no_sudo, "confirm": True,
                "jump_address": jump_address, "jump_port": jump_port,
                "jump_host_type": jump_host_type,
                "jump_credential_id": jump_credential_id,
                "jump_host_key_sha256": jump_host_key,
            }))
    elif command == "jobs":
        emit(api.request("GET", "/api/jobs"))
    elif command == "job":
        deadline = time.monotonic() + args.timeout
        while True:
            job = api.request("GET", f"/api/jobs/{args.job_id}")
            if not args.wait or job["status"] in {"succeeded", "failed", "cancelled"}:
                emit(job)
                return 1 if job["status"] == "failed" else 0
            if time.monotonic() >= deadline:
                raise SystemExit("Timed out waiting for job")
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

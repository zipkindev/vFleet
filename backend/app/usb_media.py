from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from .errors import PermanentError
from .host_upgrade import load_run, media_path


class UsbWriteRequest(BaseModel):
    plan_id: UUID
    disk_number: int = Field(ge=0, le=1024)
    friendly_name: str = Field(min_length=1, max_length=256)
    serial_number: str = Field(default="", max_length=256)
    unique_id: str = Field(default="", max_length=512)
    size_bytes: int = Field(gt=0)
    confirmation: str = Field(max_length=64)


ENUMERATE_USB = r"""
$ErrorActionPreference = 'Stop'
$rows = @(Get-Disk | Where-Object { [string]$_.BusType -eq 'USB' } | ForEach-Object {
  $disk = $_
  $letters = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue |
    Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter |
    ForEach-Object { [string]$_.DriveLetter })
  [ordered]@{
    number = [int]$disk.Number
    friendly_name = [string]$disk.FriendlyName
    serial_number = ([string]$disk.SerialNumber).Trim()
    unique_id = ([string]$disk.UniqueId).Trim()
    size_bytes = [int64]$disk.Size
    drive_letters = $letters
    is_boot = [bool]$disk.IsBoot
    is_system = [bool]$disk.IsSystem
    is_offline = [bool]$disk.IsOffline
    is_read_only = [bool]$disk.IsReadOnly
  }
})
ConvertTo-Json -InputObject $rows -Compress -Depth 4
"""


WRITER_SCRIPT = r"""
param([Parameter(Mandatory=$true)][string]$SpecPath, [switch]$Elevated)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $SpecPath -Raw | ConvertFrom-Json
$terminal = $false

function Set-VFleetStatus([string]$Phase, [int]$Percent, [string]$Message, [string]$Hash = '') {
  $body = [ordered]@{ phase=$Phase; percent=$Percent; message=$Message; sha256=$Hash; updated_at=[DateTime]::UtcNow.ToString('o') }
  $temp = "$($spec.status_path).tmp"
  $body | ConvertTo-Json -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
  Move-Item -LiteralPath $temp -Destination $spec.status_path -Force
}

try {
  if (-not $Elevated) {
    Set-VFleetStatus 'awaiting_elevation' 0 'Approve the Windows administrator prompt to write the selected USB.'
    $arguments = @(
      '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'),
      '-SpecPath', ('"' + $SpecPath + '"'), '-Elevated'
    )
    $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($child.ExitCode -ne 0) { throw "Elevated USB writer exited with code $($child.ExitCode)." }
    exit 0
  }

  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator access was not granted.'
  }

  Set-VFleetStatus 'validating' 1 'Revalidating the selected USB and installer image.'
  $disk = Get-Disk -Number ([int]$spec.disk_number)
  $serial = ([string]$disk.SerialNumber).Trim()
  $unique = ([string]$disk.UniqueId).Trim()
  if ([string]$disk.BusType -ne 'USB' -or $disk.IsBoot -or $disk.IsSystem -or $disk.IsOffline -or $disk.IsReadOnly) {
    throw 'Selected disk is no longer a writable, online, non-system USB device.'
  }
  if ([string]$disk.FriendlyName -ne [string]$spec.friendly_name -or [int64]$disk.Size -ne [int64]$spec.size_bytes) {
    throw 'USB model or size changed after selection; nothing was written.'
  }
  if ([string]$spec.serial_number -and $serial -ne [string]$spec.serial_number) {
    throw 'USB serial number changed after selection; nothing was written.'
  }
  if ([string]$spec.unique_id -and $unique -ne [string]$spec.unique_id) {
    throw 'USB unique identifier changed after selection; nothing was written.'
  }
  if (-not [string]$spec.serial_number -and -not [string]$spec.unique_id) {
    throw 'The selected USB has no stable identifier; nothing was written.'
  }

  $iso = Get-Item -LiteralPath $spec.iso_path
  if ([int64]$iso.Length -ne [int64]$spec.iso_size) { throw 'Staged ISO size changed; nothing was written.' }
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $spec.iso_path).Hash -ne [string]$spec.iso_sha256) {
    throw 'Staged ISO checksum changed; nothing was written.'
  }
  if ([int64]$disk.Size -lt [int64]$iso.Length) { throw 'The selected USB is smaller than the installer image.' }

  $letters = @(Get-Partition -DiskNumber ([int]$spec.disk_number) -ErrorAction SilentlyContinue |
    Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter })
  Set-VFleetStatus 'dismounting' 2 'Dismounting volumes on the selected USB.'
  foreach ($letter in $letters) {
    & "$env:SystemRoot\System32\mountvol.exe" ($letter + ':\') '/p' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not dismount USB volume ${letter}:; nothing was written." }
  }
  Start-Sleep -Seconds 1

  $physicalPath = '\\.\PhysicalDrive' + [string]$spec.disk_number
  $source = $null
  $destination = $null
  try {
    $source = [System.IO.FileStream]::new($spec.iso_path, 'Open', 'Read', 'Read', 1048576, 'SequentialScan')
    $destination = [System.IO.FileStream]::new($physicalPath, 'Open', 'Write', 'ReadWrite', 1048576, 'WriteThrough')
    $buffer = New-Object byte[] 1048576
    $written = [int64]0
    $lastPercent = -1
    while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
      $destination.Write($buffer, 0, $count)
      $written += $count
      $percent = [int][math]::Floor(($written * 48) / $source.Length) + 2
      if ($percent -ne $lastPercent) {
        Set-VFleetStatus 'writing' $percent "Writing verified ESXi image: $written of $($source.Length) bytes."
        $lastPercent = $percent
      }
    }
    $destination.Flush($true)
  }
  finally {
    if ($destination) { $destination.Dispose() }
    if ($source) { $source.Dispose() }
  }

  Start-Sleep -Seconds 2
  $raw = $null
  $sha = $null
  try {
    $raw = [System.IO.FileStream]::new($physicalPath, 'Open', 'Read', 'ReadWrite', 1048576, 'SequentialScan')
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $remaining = [int64]$iso.Length
    $verified = [int64]0
    $buffer = New-Object byte[] 1048576
    $lastPercent = -1
    while ($remaining -gt 0) {
      $wanted = [int][math]::Min($buffer.Length, $remaining)
      $read = $raw.Read($buffer, 0, $wanted)
      if ($read -le 0) { throw 'Unexpected end of USB while verifying.' }
      [void]$sha.TransformBlock($buffer, 0, $read, $buffer, 0)
      $remaining -= $read
      $verified += $read
      $percent = [int][math]::Floor(($verified * 49) / $iso.Length) + 50
      if ($percent -ne $lastPercent) {
        Set-VFleetStatus 'verifying' $percent "Reading back USB: $verified of $($iso.Length) bytes."
        $lastPercent = $percent
      }
    }
    [void]$sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
    $usbHash = ([BitConverter]::ToString($sha.Hash)).Replace('-', '')
  }
  finally {
    if ($sha) { $sha.Dispose() }
    if ($raw) { $raw.Dispose() }
  }
  if ($usbHash -ne [string]$spec.iso_sha256) { throw "USB read-back checksum failed: $usbHash" }
  $terminal = $true
  Set-VFleetStatus 'succeeded' 100 'Bootable ESXi installer USB written and read-back verified.' $usbHash
}
catch {
  $terminal = $true
  Set-VFleetStatus 'failed' 0 ([string]$_.Exception.Message)
  exit 1
}
"""


def _powershell() -> str:
    executable = None
    if sys.platform == "win32":
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if sys.platform != "win32" or not executable:
        runtime = os.getenv("VFLEET_RUNTIME", "").strip().lower()
        if runtime == "container":
            location = "the vFleet container image"
        elif sys.platform == "darwin":
            location = "the macOS desktop application"
        elif sys.platform.startswith("linux"):
            location = "the Linux desktop application"
        else:
            location = "this vFleet host"
        raise PermanentError(
            f"USB creation is unavailable in {location}; use the native Windows application "
            "on the computer where the USB disk is attached"
        )
    return executable


def _run_powershell(script: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermanentError("Windows PowerShell did not return USB inventory") from exc
    if result.returncode:
        raise PermanentError("Windows could not enumerate USB disks")
    return result.stdout.strip()


def list_usb_devices() -> dict:
    try:
        raw = _run_powershell(ENUMERATE_USB)
        decoded = json.loads(raw or "[]")
        rows = decoded if isinstance(decoded, list) else [decoded]
        devices = []
        for row in rows:
            serial = str(row.get("serial_number") or "").strip()
            unique = str(row.get("unique_id") or "").strip()
            safe = not any(bool(row.get(key)) for key in ("is_boot", "is_system", "is_offline", "is_read_only"))
            safe = safe and bool(serial or unique)
            letters = row.get("drive_letters") or []
            devices.append({
                "number": int(row["number"]),
                "friendly_name": str(row.get("friendly_name") or "USB disk"),
                "serial_number": serial,
                "unique_id": unique,
                "size_bytes": int(row["size_bytes"]),
                "drive_letters": [str(value) for value in (letters if isinstance(letters, list) else [letters])],
                "safe": safe,
                "blocked_reason": "" if safe else "Windows reports this USB as system, boot, offline, read-only, or lacking a stable identifier.",
            })
        return {"supported": True, "platform": "Windows PowerShell", "devices": devices,
                "message": "Raw-writing the ISO erases the complete selected USB; no separate formatting step is needed."}
    except PermanentError as exc:
        return {"supported": False, "platform": "unavailable", "devices": [], "message": str(exc)}


def validate_usb_write(store, spec: UsbWriteRequest) -> tuple[dict, Path, dict]:
    run = load_run(store, str(spec.plan_id))
    if run["phase"] not in {"planned", "complete", "aborted"}:
        raise PermanentError("Create installer media before host preparation or after the workflow is closed")
    if not run["media"].get("installer_complete") or not run["media"].get("upgrade_metadata_present"):
        raise PermanentError("The staged ISO did not pass complete ESXi installer validation")
    if spec.confirmation != f"ERASE USB DISK {spec.disk_number}":
        raise PermanentError(f"Type ERASE USB DISK {spec.disk_number} exactly to confirm the destructive write")
    if not spec.serial_number.strip() and not spec.unique_id.strip():
        raise PermanentError("The selected USB must have a stable serial number or unique identifier")
    inventory = list_usb_devices()
    if not inventory["supported"]:
        raise PermanentError(inventory["message"])
    device = next((row for row in inventory["devices"] if row["number"] == spec.disk_number), None)
    if device is None or not device["safe"]:
        raise PermanentError("The selected disk is not a safe removable USB target")
    expected = (spec.friendly_name, spec.serial_number.strip(), spec.unique_id.strip(), spec.size_bytes)
    actual = (device["friendly_name"], device["serial_number"], device["unique_id"], device["size_bytes"])
    if actual != expected:
        raise PermanentError("USB identity changed after selection; rescan before writing")
    path = media_path(store, run["staging_id"])
    return run, path, device


def _windows_temp_paths(job_id: str) -> tuple[Path, dict[str, str]]:
    root_win = _run_powershell("[IO.Path]::GetTempPath()")
    if os.name == "nt":
        root = Path(root_win)
        convert = lambda value: str(value)
    else:
        try:
            root_text = subprocess.run(["wslpath", "-u", root_win], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise PermanentError("Could not translate the Windows temporary directory from WSL") from exc
        root = Path(root_text)
        def convert(value: Path) -> str:
            try:
                return subprocess.run(["wslpath", "-w", str(value)], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                raise PermanentError("Could not translate USB staging paths for Windows") from exc
    work = root / ("vfleet-usb-" + str(UUID(job_id)))
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = {name: convert(work / filename) for name, filename in {
        "iso": "installer.iso", "script": "write-usb.ps1", "spec": "spec.json", "status": "status.json"
    }.items()}
    return work, paths


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_installer_usb(store, job, sleep=time.sleep) -> dict:
    try:
        spec = UsbWriteRequest.model_validate(job.payload)
        run, source, device = validate_usb_write(store, spec)
        work, paths = _windows_temp_paths(job.id)
        status_path = work / "status.json"
        launched = bool(job.progress.get("launched"))
        if not launched:
            target = work / "installer.iso"
            store.save_progress(job.id, {"phase": "staging", "percent": 0, "message": "Copying verified ISO to Windows staging."})
            shutil.copyfile(source, target)
            if target.stat().st_size != run["media"]["size"] or _hash(target).lower() != run["media"]["sha256"]:
                raise PermanentError("Windows staging copy failed ISO checksum verification")
            (work / "write-usb.ps1").write_text(WRITER_SCRIPT, encoding="ascii")
            payload = {
                "disk_number": spec.disk_number, "friendly_name": spec.friendly_name,
                "serial_number": spec.serial_number.strip(), "unique_id": spec.unique_id.strip(),
                "size_bytes": spec.size_bytes, "iso_path": paths["iso"], "iso_size": run["media"]["size"],
                "iso_sha256": run["media"]["sha256"].upper(), "status_path": paths["status"],
            }
            (work / "spec.json").write_text(json.dumps(payload), encoding="utf-8")
            store.save_progress(job.id, {"phase": "launching", "percent": 0, "message": "Launching guarded Windows USB writer.", "launched": True})
            try:
                process = subprocess.Popen(
                    [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", paths["script"], "-SpecPath", paths["spec"]],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise PermanentError("Could not launch the Windows USB writer") from exc
        else:
            process = None
            if not status_path.exists():
                raise PermanentError("USB write launch state is uncertain; rescan the device and explicitly start a new write")

        deadline = time.monotonic() + 7200
        last = {}
        while time.monotonic() < deadline:
            if status_path.is_file():
                try:
                    current = json.loads(status_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    current = {}
                if current and current != last:
                    last = current
                    store.save_progress(job.id, dict(current, launched=True))
                if current.get("phase") == "succeeded":
                    for name in ("installer.iso", "write-usb.ps1", "spec.json"):
                        (work / name).unlink(missing_ok=True)
                    return {"plan_id": run["id"], "disk_number": spec.disk_number,
                            "friendly_name": device["friendly_name"], "size_bytes": spec.size_bytes,
                            "sha256": current.get("sha256", "").lower(), "verified": True,
                            "message": "Bootable ESXi installer USB written and read-back verified."}
                if current.get("phase") == "failed":
                    raise PermanentError(str(current.get("message") or "Windows USB writer failed"))
            if process is not None and process.poll() is not None and process.returncode != 0 and not status_path.exists():
                raise PermanentError("Windows elevation was cancelled or the USB writer could not start")
            sleep(1)
        raise PermanentError("Timed out waiting for the Windows USB writer; inspect the selected USB before retrying")
    except PermanentError:
        raise
    except Exception as exc:
        raise PermanentError(f"USB writer stopped safely: {type(exc).__name__}") from exc

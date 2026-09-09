from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

from .errors import PermanentError

SECTOR = 2048


def inspect_esxi_iso(path: Path) -> dict:
    """Read ISO9660 metadata without mounting, extracting, or executing the image."""
    size = path.stat().st_size
    if not 64 * 1024 <= size <= 16 * 1024**3:
        raise PermanentError("Installer ISO size is outside the supported range")
    with path.open("rb") as stream:
        def read(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset + length > size:
                raise PermanentError("ISO contains an out-of-bounds extent")
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise PermanentError("ISO is truncated")
            return data

        descriptor = None
        for block in range(16, 48):
            item = read(block * SECTOR, SECTOR)
            if item[1:6] != b"CD001" or item[6] != 1:
                raise PermanentError("Not a supported ISO9660 installer image")
            if item[0] == 1:
                descriptor = item
                break
            if item[0] == 255:
                break
        if descriptor is None or struct.unpack_from("<H", descriptor, 128)[0] != SECTOR:
            raise PermanentError("ISO9660 primary volume descriptor is missing or unsupported")

        def extent(record: bytes) -> tuple[int, int]:
            if len(record) < 34 or record[0] > len(record):
                raise PermanentError("Invalid ISO directory record")
            block, length = struct.unpack_from("<I", record, 2)[0], struct.unpack_from("<I", record, 10)[0]
            if block != struct.unpack_from(">I", record, 6)[0] or length != struct.unpack_from(">I", record, 14)[0]:
                raise PermanentError("Inconsistent ISO extent metadata")
            if record[25] & 0x80:
                raise PermanentError("Multi-extent ISO files are not supported")
            return block * SECTOR, length

        def directory(record: bytes) -> dict[str, bytes]:
            offset, length = extent(record)
            if length > 4 * 1024**2:
                raise PermanentError("ISO directory is too large")
            data = read(offset, length)
            rows: dict[str, bytes] = {}
            pos = 0
            while pos < len(data):
                count = data[pos]
                if count == 0:
                    pos = (pos // SECTOR + 1) * SECTOR
                    continue
                item = data[pos:pos + count]
                extent(item)
                name_length = item[32]
                if 33 + name_length > count:
                    raise PermanentError("Invalid ISO filename record")
                name = item[33:33 + name_length].decode("ascii", errors="strict").split(";", 1)[0].upper()
                if name not in {"\x00", "\x01"}:
                    if name in rows:
                        raise PermanentError("Ambiguous duplicate ISO filename")
                    rows[name] = item
                pos += count
            return rows

        root = directory(descriptor[156:190])

        def file_bytes(rows: dict[str, bytes], name: str, limit: int) -> bytes:
            record = rows.get(name)
            if record is None or record[25] & 2:
                raise PermanentError(f"Installer ISO is missing required file {name}")
            offset, length = extent(record)
            if length <= 0 or length > limit:
                raise PermanentError(f"Installer ISO file {name} has an invalid size")
            return read(offset, length)

        cfg = root.get("BOOT.CFG")
        if cfg is None or cfg[25] & 2:
            raise PermanentError("Installer ISO must contain a root boot.cfg file")
        offset, length = extent(cfg)
        if length > 128 * 1024:
            raise PermanentError("Installer boot.cfg is too large")
        text = read(offset, length).decode("utf-8", errors="strict")
        builds = re.findall(r"(?m)^build=(\d+\.\d+\.\d+)-[^\r\n]*?(\d{8})\s*$", text)
        if len(builds) != 1 or not re.search(r"(?m)^kernelopt=.*\brunweasel\b", text):
            raise PermanentError("Cannot identify a unique ESXi installer build in boot.cfg")
        # Ensure the declared boot kernel exists. Metadata identifies the target;
        # authenticity still requires comparison with the publisher's checksum.
        kernels = re.findall(r"(?m)^kernel=([^\r\n]+)", text)
        if len(kernels) != 1 or kernels[0].strip().lstrip("/").upper() not in root:
            raise PermanentError("Installer kernel referenced by boot.cfg is missing")

        module_lines = re.findall(r"(?m)^modules=([^\r\n]+)", text)
        if len(module_lines) != 1:
            raise PermanentError("Installer boot.cfg must declare one module list")
        modules = []
        for item in module_lines[0].split("---"):
            name = item.strip().split(maxsplit=1)[0].lstrip("/").upper()
            if not name or "/" in name or "\\" in name:
                raise PermanentError("Installer boot.cfg contains an unsupported module path")
            record = root.get(name)
            if record is None or record[25] & 2 or extent(record)[1] <= 0:
                raise PermanentError(f"Installer boot module {name} is missing or empty")
            modules.append(name)
        required_modules = {"WEASELIN.V00", "ESXUPDT.V00", "IMGDB.TGZ", "IMGPAYLD.TGZ"}
        if not required_modules.issubset(modules):
            raise PermanentError("Installer ISO is missing required installer or image-payload modules")

        upgrade_record = root.get("UPGRADE")
        if upgrade_record is None or not upgrade_record[25] & 2:
            raise PermanentError("Installer ISO is missing its UPGRADE metadata directory")
        upgrade = directory(upgrade_record)
        required_upgrade = {"ESXIMAGE.ZIP", "METADATA.XML", "METADATA.ZIP", "PRECHECK.PY", "PREP.PY", "PROFILE.XML"}
        missing_upgrade = sorted(
            name for name in required_upgrade
            if name not in upgrade or upgrade[name][25] & 2 or extent(upgrade[name])[1] <= 0
        )
        if missing_upgrade:
            raise PermanentError("Installer ISO is missing required UPGRADE files: " + ", ".join(missing_upgrade))

        metadata = file_bytes(upgrade, "METADATA.XML", 1024 * 1024).decode("utf-8", errors="strict")
        profile = file_bytes(upgrade, "PROFILE.XML", 4 * 1024 * 1024).decode("utf-8", errors="strict")
        metadata_versions = re.findall(r"<esxVersion>\s*([^<]+)\s*</esxVersion>", metadata)
        metadata_builds = re.findall(r"<build>\s*(\d+)\s*</build>", metadata)
        profile_names = re.findall(r"<name>\s*([^<]+)\s*</name>", profile)
        if metadata_versions != [builds[0][0]] or metadata_builds != [builds[0][1]] or len(profile_names) != 1:
            raise PermanentError("Installer boot and upgrade metadata do not identify the same unique image")
        if builds[0][1] not in profile_names[0]:
            raise PermanentError("Installer image profile does not match the declared build")
        for name in ("ESXIMAGE.ZIP", "METADATA.ZIP"):
            record = upgrade[name]
            offset, length = extent(record)
            if read(offset, min(length, 4)) != b"PK\x03\x04":
                raise PermanentError(f"Installer ISO contains an invalid {name} archive")

        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"kind": "installer_iso", "version": builds[0][0], "build": builds[0][1],
            "sha256": digest, "size": size, "method": "assisted_iso",
            "free_edition": builds[0][1] == "24677879", "installer_complete": True,
            "boot_module_count": len(modules), "upgrade_metadata_present": True,
            "image_profile": profile_names[0], "automation_mode": "guided_physical_boot"}

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


repo = Path.cwd()
hidden_imports = collect_submodules("uvicorn")

analysis = Analysis(
    [str(repo / "packaging" / "desktop_entry.py")],
    pathex=[str(repo / "backend")],
    binaries=[],
    datas=[
        (str(repo / "frontend" / "dist"), "frontend/dist"),
        (str(repo / "CHANGELOG.md"), "."),
        (str(repo / "VERSION"), "."),
        (str(repo / "LICENSE"), "."),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="vfleet-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    hide_console="hide-early",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

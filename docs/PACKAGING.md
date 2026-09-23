# Packaging and installation

vFleet 1.3 packages the existing React/FastAPI modular monolith in two forms: a native Tauri desktop application and a non-root OCI container. The React application is still served by FastAPI on the same origin; no remote operator actions were moved into the desktop shell.

## Release artifacts

| Platform | Release output | Persistent application data |
| --- | --- | --- |
| Windows x86-64 | NSIS `.exe` installer and MSI installer | `%LOCALAPPDATA%\vFleet` |
| macOS Apple silicon and Intel | `.dmg` | `~/Library/Application Support/vFleet` |
| Linux x86-64 | `.deb` and `.rpm` | `${XDG_DATA_HOME:-~/.local/share}/vfleet` |
| Docker/OCI, amd64 and arm64 | `ghcr.io/zipkindev/vfleet:<version>` | `/var/lib/vfleet`; key in `/var/lib/vfleet-key` |

The desktop application starts a bundled Python sidecar on an ephemeral `127.0.0.1` port, waits for its ASGI lifespan to be ready, and then navigates the native webview to that loopback origin. An idle close shuts the backend down gracefully; if jobs are queued or running, vFleet displays a native warning before a forced close. Persistent jobs are re-queued at the next launch, but a VMware task already accepted remotely can continue independently.

## Verify a download

Every GitHub release contains `SHA256SUMS`. Download the package and the manifest from the same release, calculate the package digest, and compare it with that artifact's entry before installing:

```bash
# Linux
sha256sum ./linux-x86_64-vFleet_1.3.0_amd64.deb

# macOS
shasum -a 256 ./macos-aarch64-vFleet_1.3.0_aarch64.dmg
```

On Windows PowerShell, compare this output with the matching manifest entry:

```powershell
Get-FileHash .\vFleet-*.exe -Algorithm SHA256
```

### Unsigned initial releases

The initial desktop artifacts are not signed with a Microsoft code-signing certificate or an Apple Developer ID, and the macOS application is not notarized. Windows SmartScreen and macOS Gatekeeper may therefore warn or block the package. This is a distribution limitation, not a claim that warnings can be ignored: verify the checksum and that the download came from `zipkindev/vFleet` before using the operating system's explicit **Run anyway** or **Open Anyway** control. Enterprise policy may forbid unsigned software; use the container or build from the reviewed tag in that case.

Linux packages are also unsigned. Use the package manager appropriate for the downloaded `.deb` or `.rpm` and review its local install summary before confirming.

## Container installation

The OCI artifact targets `linux/amd64` and `linux/arm64`. It runs directly with Docker Engine on Linux and through Docker Desktop's Linux-container runtime on Windows or macOS; it is not a native Windows-container image.

The supplied Compose configuration keeps the port on loopback, drops all Linux capabilities, enables `no-new-privileges`, uses a read-only root filesystem, runs as UID/GID 10001, and persists the encrypted records separately from their master key.

```bash
export UI_TOKEN='generate-a-long-random-value'
docker compose pull
docker compose up -d
```

Open `http://127.0.0.1:8080`, paste the same value into **UI token**, and then connect to vSphere. The image deliberately refuses to start with an empty `UI_TOKEN`. Keep exactly one container replica: the SQLite store and relay are single-writer components, and multiple workers could execute the same queue incorrectly.

If you invoke `docker run` directly, preserve both volumes and publish only to loopback:

```bash
docker run -d --name vfleet --init --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m \
  -p 127.0.0.1:8080:8080 \
  -e UI_TOKEN="$UI_TOKEN" \
  -v vfleet-data:/var/lib/vfleet \
  -v vfleet-key:/var/lib/vfleet-key \
  ghcr.io/zipkindev/vfleet:1.3.0
```

USB installer creation is intentionally unavailable in macOS, Linux, and container builds. It remains available only through the native Windows application (and the existing explicitly supported Windows PowerShell path) because the writer revalidates Windows disk identity and uses an elevated raw-device workflow.

## Build native packages from source

Install Python 3.12, Node.js 22, stable Rust, and the [Tauri v2 platform prerequisites](https://v2.tauri.app/start/prerequisites/). Linux additionally needs WebKitGTK 4.1, Ayatana AppIndicator, librsvg, libxdo, OpenSSL, and `patchelf` development packages.

```bash
python -m pip install -r backend/requirements-dev.txt
npm ci --prefix frontend
npm ci
npm run build --prefix frontend
python scripts/build-sidecar.py
python scripts/smoke-sidecar.py
# Choose the bundle family for this host:
npm run desktop:build -- --bundles deb,rpm  # Linux
# npm run desktop:build -- --bundles dmg    # macOS
# npm run desktop:build -- --bundles nsis,msi  # Windows
```

PyInstaller creates a single sidecar named with the current Rust target triple, which Tauri embeds through `bundle.externalBin`. Native packages are never cross-compiled in CI: Windows, macOS, and Linux each build on a matching GitHub-hosted runner.

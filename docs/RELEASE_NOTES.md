vFleet is available as a native Windows, macOS, or Linux desktop application and as a multi-architecture container image.

The initial native artifacts are **not code-signed or notarized**. Windows SmartScreen and macOS Gatekeeper can therefore warn or block the download. Verify the matching entry in `SHA256SUMS` before installing, and follow the platform-specific instructions in [Packaging and installation](https://github.com/zipkindev/vFleet/blob/main/docs/PACKAGING.md).

Container images are published at `ghcr.io/zipkindev/vfleet` for `linux/amd64` and `linux/arm64`. Keep both `/var/lib/vfleet` and `/var/lib/vfleet-key` on separate persistent volumes and publish the application port to loopback unless you have deliberately secured a remote deployment.

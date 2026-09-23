# Security policy

## Supported versions

Security fixes are made on the latest release line and on `main`. Older desktop installers and container tags are not maintained once a newer feature release is available.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability, exposed credential, console ticket, datastore path, or infrastructure detail. Use GitHub's [private vulnerability reporting form](https://github.com/zipkindev/vFleet/security/advisories/new) instead. Include the affected version, deployment mode, reproduction steps, impact, and any safe proof-of-concept material. Remove real vCenter/ESXi names, usernames, addresses, certificates, tickets, and secrets from the report.

You should receive an acknowledgement within seven days. A fix timeline depends on severity and whether the issue touches VMware behavior that requires operator validation. Coordinated disclosure is preferred.

## Deployment boundary

vFleet is a local operator tool, not a hardened multi-tenant control plane. Keep the desktop backend on loopback. The container requires a non-empty `UI_TOKEN` and its example publishes only to `127.0.0.1`; use an authenticated TLS reverse proxy and explicit network controls before any deliberate remote exposure. VMware permissions remain the final authority for remote operations.

Never attach production databases, `.env` files, encrypted credential records, master keys, installer media, support bundles, or logs containing infrastructure identities to a public report.

## Tracked upstream dependency risk

The Linux desktop package currently receives `glib` 0.18.5 through Tauri 2's GTK3 stack. RustSec [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html) reports unsoundness in `glib::VariantStrIter`; its fixed release line requires the [GTK4/WebKitGTK 6 migration](https://github.com/tauri-apps/tauri/pull/14684) being tracked upstream by Tauri. vFleet does not call `VariantStrIter`, and the locked dependency sources contain no non-test callers, so this is treated as a constrained transitive risk rather than shipping an unreviewed framework fork. Windows, macOS, and container builds do not compile this Linux GTK dependency. Re-evaluate the advisory with every Tauri update and remove this exception as soon as the stable Linux stack adopts a fixed `glib` line.

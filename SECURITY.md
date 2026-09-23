# Security policy

## Supported versions

Security fixes are made on the latest release line and on `main`. Older desktop installers and container tags are not maintained once a newer feature release is available.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability, exposed credential, console ticket, datastore path, or infrastructure detail. Use GitHub's [private vulnerability reporting form](https://github.com/zipkindev/vFleet/security/advisories/new) instead. Include the affected version, deployment mode, reproduction steps, impact, and any safe proof-of-concept material. Remove real vCenter/ESXi names, usernames, addresses, certificates, tickets, and secrets from the report.

You should receive an acknowledgement within seven days. A fix timeline depends on severity and whether the issue touches VMware behavior that requires operator validation. Coordinated disclosure is preferred.

## Deployment boundary

vFleet is a local operator tool, not a hardened multi-tenant control plane. Keep the desktop backend on loopback. The container requires a non-empty `UI_TOKEN` and its example publishes only to `127.0.0.1`; use an authenticated TLS reverse proxy and explicit network controls before any deliberate remote exposure. VMware permissions remain the final authority for remote operations.

Never attach production databases, `.env` files, encrypted credential records, master keys, installer media, support bundles, or logs containing infrastructure identities to a public report.

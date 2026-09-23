# Updating vFleet

vFleet does not silently self-update. Native builds have no updater endpoint or signing key configured; operators choose when to replace the installed application. Container deployments move only when an explicit tag is pulled or the Compose image is changed.

## Before updating

1. Open **Jobs** and wait until queued, retrying, and running counts are zero. A native close prompt warns when work is still open, but finishing the work is safer than interrupting it.
2. Back up the application data and the separate credential master key as described in [Backup and restore](BACKUP_RESTORE.md).
3. Read the release notes and compare `SHA256SUMS` with the downloaded artifact.
4. Record the installed version from the UI. Do not assume a cached browser tab represents the newly installed backend.

## Native application

Close vFleet, install the new package over the prior version, and launch it. Mutable data is outside the installation directory, so installers do not need to copy the SQLite queue or credential records. Verify `/api/version` through the UI's displayed version, confirm the expected connection profile exists, and review Jobs before starting new work.

Rollback means reinstalling the earlier verified artifact and restoring the backup taken before the update. Database changes are designed to be additive, but an older binary is not guaranteed to understand state written by a newer feature release; restore the matching backup instead of pointing an older build at newer data.

## Container

Use immutable version tags in operational deployments:

```bash
docker compose stop vfleet
docker compose pull vfleet
docker compose up -d vfleet
docker compose ps
```

Confirm the `/api/health` result and the version shown in the UI. Do not scale the service above one replica. To roll back, stop the service, restore the matching data and key backups, select the earlier image tag, and start one replica.

The `latest` tag is convenient for evaluation but is not a reproducible deployment reference. Release tags and image digests are preferable for change control.

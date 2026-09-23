# Backup and restore

vFleet state has two security domains:

- The data directory contains `vfleet.db` (jobs, cached inventory, metrics, and upgrade state), staged transfers, connection metadata, and encrypted credential records.
- The master key defaults to `~/.vfleet/credential.key` for source/native use, or the path selected by `VFLEET_MASTER_KEY_FILE`. Containers use `/var/lib/vfleet-key/credential.key` on a separate volume.

The encrypted JSON files are not recoverable without the matching key. Keeping the key beside the backup protects against disk loss but does not protect against backup disclosure, so store and access-control them separately.

## Consistent backup

1. Wait for Jobs to report zero queued, retrying, or running work. Complete or deliberately resolve any active ESXi upgrade reservation.
2. Close the native application or stop the single container. Do not copy a live SQLite database without a SQLite-aware snapshot because WAL data may not yet be in the main file.
3. Copy the complete data directory, including staging contents if an upload must be retained.
4. Copy the master-key file to a separate protected location.
5. Record the vFleet version and platform with the backup. Encrypt backups at rest and test restore access without using production credentials.

Container operators should stop the service and snapshot/export both `vfleet-data` and `vfleet-key`. Volume tooling differs by host; ensure the archive preserves files owned by UID/GID 10001 and mode `0600`/`0700` where applicable.

## Restore

1. Install or select the same vFleet version recorded with the backup.
2. Ensure vFleet is stopped and the target directories are empty or separately preserved.
3. Restore the data directory and master key to their original paths. Restrict key access to the vFleet user.
4. Start one backend instance, open Jobs, and review any re-queued work before reconnecting to vSphere.
5. Test decryption by selecting a saved profile without exposing the secret. Validate endpoint fingerprints before allowing a durable job to resume.

Never merge databases, credential JSON, or key files from unrelated installations. Restoring only `vfleet.db` does not restore saved credentials; restoring only encrypted JSON without its original key makes those records unusable.

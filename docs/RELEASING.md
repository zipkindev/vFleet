# Release procedure

Releases are deliberately gated because vFleet can perform destructive infrastructure operations.

1. Work on a release branch while the repository is private. Synchronize `VERSION`, backend/frontend/native package metadata, and the changelog.
2. Push the branch and require the complete private `CI` workflow to pass: all backend tests, dependency audits, frontend typecheck/build, hardened container health/auth and vulnerability/secret checks, packaged Python sidecar smoke, and native Tauri packaging on Windows, Linux, macOS Apple silicon, and macOS Intel.
3. Review the CI artifacts privately. At minimum, install or extract each platform family, verify startup in demo mode, verify persisted data is outside the application, and confirm active-job close warnings. Adapter changes still require a controlled VMware environment; demo tests are not live-vCenter proof.
4. Merge the reviewed branch only after required checks are green. Make the repository public, enable private vulnerability reporting, and set the repository description/topics/social preview.
5. Create and push the signed or annotated version tag, for example `v1.3.0`. The Release workflow re-runs source validation and all native builds. It refuses publication while the repository is private.
6. Only after native jobs pass does the workflow publish the multi-architecture GHCR image. It verifies an anonymous pull before creating the GitHub release.
7. The GitHub release is created as a draft, populated with every artifact plus `SHA256SUMS`, and made public only in the final step. Monitor the workflow through completion and smoke the public downloads and container tag.

## Signing status

The 1.3.0 automation does not configure Microsoft Authenticode, Apple Developer ID signing/notarization, Linux package signing, or Tauri updater signatures. Release notes and installation docs label the artifacts unsigned. Do not change that language until the relevant credentials are stored as protected GitHub environment secrets, the workflow performs signing/notarization on the correct runners, and verification commands pass against the published artifacts.

## Failure and rerun behavior

A failed native job prevents container and GitHub release publication. A failed anonymous GHCR pull prevents the release; if GitHub created the package as private, an administrator must make it public in the package settings before rerunning the failed job. A failure after draft creation leaves the release non-public. Reruns may replace assets on that draft with `--clobber`; the workflow refuses to replace assets after publication and retains the immutable Git tag as the source of record.

Do not delete or overwrite a published version tag. Correct material release defects with a new patch release and describe the superseded artifact.

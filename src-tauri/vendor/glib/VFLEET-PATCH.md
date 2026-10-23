# vFleet GLib safety backport

This directory contains the published `glib` 0.18.5 crate (MIT licensed; see
COPYRIGHT and LICENSE) with the upstream fix for RUSTSEC-2024-0429 /
GHSA-wrw7-89jp-8q8g backported from:

https://github.com/gtk-rs/gtk-rs-core/pull/1343

Published crate archive SHA-256:
`233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5`.
All published crate files are preserved byte-for-byte except the two source
lines below and the omitted registry `.cargo_vcs_info.json` metadata file.

The only upstream source change is in `src/variant_iter.rs`: the output pointer
in `VariantStrIter::impl_get` is mutable and is passed to `g_variant_get_child`
as `&mut p`. Passing an immutable reference allowed optimized builds to treat
the pointer as permanently null, causing undefined behavior.

The package version remains 0.18.5 to reflect its actual API. GTK3 requires the
0.18 series, so adding glib 0.20 alongside it would leave the affected copy in
the dependency graph. The path override in the desktop Cargo.toml replaces
that copy for every consumer. Do not remove the override without verifying
that the resolved Linux GTK stack uses a fixed implementation.

`src-tauri/tests/glib_variant_str_iter.rs` exercises the affected iterator in
both directions. Run it with optimizations to cover the original failure:

    cargo test --manifest-path src-tauri/Cargo.toml --locked --release --test glib_variant_str_iter

Remove this vendored crate and its override when the GTK/Tauri dependency stack
supports an upstream fixed version. GitHub's version-based Dependabot alert
may still require a manual review of this backport; do not mislabel this copy
as upstream glib 0.20 or dismiss an alert as fixed solely on a version change.

Cargo-audit excludes local path dependencies from registry advisory matching.
Its clean result therefore does not validate this backport: the source comparison
and optimized regression test provide that validation. No advisory is ignored.

# PK01 — Versioned project source inspection

Implemented on top of the packaging proposal; PR/CI completion is tracked by the
implementation PR. [User contract](../handbook/project-inspection.md) ·
[Packaging design](project-packaging.md) ·
[Remaining slices](../plans/project-packaging-implementation.md).

The new `projects` module reads format-1 identity or a strict format-2 declaration
into a canonical, bounded resource graph. It has no Store, runtime, worker or
network dependency. `ProjectSourceCommand` is an offline API, deliberately distinct
from daemon `Action`: registration of a source definition must not create a runtime
project. CLI and the fixed-worktree MCP adapter use the same handler.

`ProjectConfig::read` remains the runtime gate and refuses format 2 with an explicit
inspection-only error. `init` still emits format 1. MCP can bind public identity
for inspection, then checks that identity on each successful report. Existing
daemon binding also validates the manifest before registering a runtime project;
forged or copied format-2 identities cannot bypass that gate. Catalog schema 10
and the existing runtime installation are unchanged by inspection.

Declared resource fragments merge without override semantics. Typed nodes carry
logical resource keys and dependencies; a deterministic DFS returns dependencies
before dependants and refuses cycles/missing keys. Unknown kinds/capabilities,
security fields, interpolation and executable hooks fail closed. Target selection
is deterministic; production mode is metadata only. Explicitly referenced resource
and environment files must be included in the inventory. Notebook JSON uses the
existing document validator; dependency declarations are structurally checked and
hashed, with no claim of uv freshness, kernel compatibility or offline closure.

Reads reuse the notebook subsystem's descriptor-relative directory primitive.
Every path component is opened without following symlinks. Reads are bounded,
regular single-link files; device/inode, size, ctime and mtime are checked around
reads and by reopening selected paths before success. Case-folded path prefixes
catch collisions between directories as well as files. This prevents observed
substitution from silently changing an input; it is not an atomic filesystem
snapshot against arbitrary concurrent editors. Apply in PK04 must fence/revalidate
its exact input set. Only declared files and resource fragments are read, and no
source file is written or executed.

The inventory digest is SHA-256 over compact key-sorted JSON of relative paths to
`{bytes, sha256}` entries. It is independent of absolute roots and target selection.
The checked JSON report fixture must match on Linux and macOS. Schemas validate
parsed TOML and the public JSON contract; filesystem/graph semantics are enforced
by Rust. No new runtime dependencies are introduced.

Alpha.18 ships the schemas, user guide and inspection-only sales example. The
installed qualification harness inspects and validates that example before
runtime startup, verifies identical reports, absent state creation and unchanged
release inventory, and confirms execution binding is refused. All prior native
release gates remain. PK02 packaging, PK03 deployment binding, UC and IAM are not
implemented by this slice.

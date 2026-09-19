# PK02 — Deterministic source packages

Implemented contract; qualification and merge status are tracked in the implementation
PR. [User workflow](../handbook/project-packages.md) ·
[Remaining slices](../plans/project-packaging-implementation.md).

The platform owns source packaging. The console submodule, database engine and
catalog schema remain unchanged. PK02 adds `project pack`, archive `project
inspect`, `project verify`, and `project unpack`, plus explicit saved-query export.
Only format-2 definitions can be packaged; format-1 runtime projects are not
silently converted. All archives use the `source` profile. Deployment resolution,
apply, dependency installation and offline wheel closures remain PK03–PK05 work.

## Version 1 artifact

`.sbproj` is a single gzip member containing a canonical USTAR stream:

```text
package.json
project/supabricks.toml
project/<declared resource fragments and selected files>
```

There are no directory entries, links, PAX/GNU extensions, executable bits or
hooks. Files appear in bytewise path order with mode 0600, UID/GID/mtime zero,
empty owner names and zero padding. The stream ends with exactly two zero blocks.
Gzip has mtime zero, OS byte 255 and no filename/comment. The pinned pure Rust
flate2 backend produces repeatable bytes across the supported native targets.
Highly compressible input falls back to stored DEFLATE blocks to remain within
the reader's expansion-ratio budget. Inputs whose paths cannot be represented in
USTAR fail with an instruction to shorten names; no extension records are emitted.

`package.json` is compact, recursively key-sorted JSON with `content` and
`content_sha256`. Content contains version 1, profile `source`, the full PK01
inspection report computed from the **packaged** bytes, and the fixed exclusion
policy. Its digest is SHA-256 of canonical content JSON, including the complete
payload inventory/hashes and resource graph. The transport digest is SHA-256 of
all gzip bytes and is returned by the command, outside the archive. No digest
includes itself. Hashes establish integrity, not publisher identity or code trust.
The package and report schemas ship in `schemas/`.

The default selected target is recorded at pack time. An inspection/unpack target
override changes the returned graph selection, never the package's content digest
or any permissions. All targets and inferred capabilities remain visible.

## Validation and publication

The reader bounds compressed and expanded archive bytes to 40 MiB and expansion
to 200:1. Metadata is at most 2 MiB; the project is at most 1,024 files / 32 MiB,
with PK01's 8 MiB file, 1 MiB TOML/lock, path, graph and include bounds retained.
It checks gzip CRC/EOF and rejects trailing data or extra members. It accepts only
regular USTAR entries, checks count/size before reading payload, rejects duplicates
and unsafe paths, and rebuilds the normalized tar stream to reject metadata,
ordering, padding or terminator ambiguity. The in-memory source validator rejects
case collisions, file/directory conflicts, undeclared payloads, changed hashes,
unknown capabilities and graph mismatches. Inspection does not extract files,
start a daemon, invoke executables, resolve dependencies or access a network.

Packing retains bounded bytes read through PK01's descriptor-relative source
reader, transforms the copies, then revalidates observed source file identities
before publication. This is not an atomic snapshot against arbitrary concurrent
editors. The packaged inventory always describes the bytes actually published.

Packing and query export stage in a private sibling directory and publish a file
with no-replace rename. Unpack verifies everything first, writes through directory
descriptors into a new 0700 staging directory (files 0600), syncs contents, writes
`supabricks-unpacked.json` last, then publishes the whole directory with no-replace
rename and syncs the parent. Errors preserve any existing destination; ordinary
failures remove staging. SIGKILL can leave a private `.supabricks-package-*` staging
directory, which is never a completed destination. The marker records provenance
and `unbound`; it confers no authority and cannot be supplied in an archive.
Format-2 runtime admission remains blocked centrally until PK03.

## Portable contents and saved queries

Only selected files and explicit manifest fragments enter the inventory. Known
private/config/key/cache/environment and runtime-binary paths fail if selected;
unselected files are not scanned. TOML URI values (including uv sources/indexes)
with userinfo or recognized credential query parameters are rejected. This is
not a general secret detector for code, SQL, fixtures or arbitrary metadata.

Every selected `.ipynb` is validated and normalized. Code-cell outputs and execution
counts are reset; widget state, execution timing/trust metadata and Supabricks
runtime metadata are removed at notebook and cell level. The source files remain
byte-identical. Markdown, code, attachments and ordinary author metadata remain.

`SavedQueryExport` is a project-scoped daemon API and read-only MCP tool. It requires
an explicit query UUID and expected revision, reads one bounded private record
through directory descriptors, and returns SQL/title/revision without branch or
runtime bindings. The CLI writes only that SQL to a new explicit destination.
Private originals and revisions are unchanged. Exported files must then be
explicitly selected in the format-2 manifest; packaging never reads the private
query store. Existing saved queries are PostgreSQL queries.

## Evidence

Portable tests cover a fixed archive/content digest across roots and targets,
source-preserving stripping, no runtime/tool/HOME dependency, admission guards,
scoped/revision-fenced query export, private paths, credential URIs, corrupt hashes,
links, traversal, duplicates, case conflicts, bombs, extra gzip members, hidden tar
trailers, interrupted staging and destination races. Alpha.19 qualification adds
an installed pack/verify/inspect/unpack round trip before runtime startup on both
native targets and retains the existing release gates.

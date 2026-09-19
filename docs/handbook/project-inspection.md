# Inspect a project definition (PK01 preview)

PK01 adds offline source validation and inspection. [PK02 source packages](project-packages.md)
add archive creation, verification and unpacking. Deployment, migrations and Unity Catalog remain later work.
Your existing format-1 `supabricks.toml` and running projects continue to work.

```sh
supabricks project validate --project /absolute/project
supabricks project inspect --project /absolute/project --target local
```

Both commands return the same versioned JSON report and have the same validation
rules. They read source files only: no daemon startup, state directory creation,
network, dependency resolution, SQL execution or notebook execution. A `--data-dir`
argument is accepted for CLI consistency but never accessed. `--project` can be
omitted to discover a manifest in the current directory or an ancestor.

The native release ships an inspection-only example:

```sh
supabricks project inspect \
  --project "$HOME/.local/share/supabricks/current/examples/projects/sales"
```

The report includes public definition identity, selected target, typed resources,
deterministic dependency order, inferred/declared capabilities, file sizes and
SHA-256 hashes, notebook dependency input hashes and unresolved database bindings.
Paths are project-relative; it includes no host paths, source contents or runtime
credentials. `source_sha256` hashes the compact, key-sorted JSON file inventory.
The inspection schema is version 1; CLI API version remains 1. `valid` means source
validation passed, not that dependencies are fresh, offline-ready or compatible
with the installed kernel. The report states those limits explicitly.

## Format compatibility

Format 1 remains supported. [PK03](project-deployments.md) adds explicit deployment binding for format 2. Its inspector inventories only
`supabricks.toml`; it does not guess which application files to package. The normal
`init` command still writes format 1 and never rewrites an existing project.

Format 2 is a preview definition of portable resources. An unbound copy cannot run,
even if its UUID matches an existing project. PK03 requires explicit create, attach
or adoption. Source inspection alone never creates that binding. A target named `production` or a UUID in
a source file confers no authorization.

Format-2 manifests require `id`, `name`, `package.version` (SemVer),
`package.include` and `package.notebook_outputs = "strip"`. PK02 applies stripping in the
packaged copy; source inspection preserves notebooks and their outputs.
The supported resource groups/kinds are:

| Group | Kind | Required reference fields |
| --- | --- | --- |
| `database` | `postgres_database` | `lifecycle = "retain"` |
| `query` | `sql` | `file`, `engine = "postgres"` or `"spark"`, `database = "database.<key>"` |
| `notebook` | `notebook` | `file`, `environment`, `database = "database.<key>"` |

Resources may declare `depends_on` logical keys. Database references also create
graph edges. Cycles, undeclared references and duplicate resource keys fail.
Environments name sibling `pyproject.toml` and `uv.lock` paths inside the project,
including the project root if explicitly selected. Locks must parse as uv version
1 with a package array. PK01 does not run uv or validate dependency resolution.

`requires.capabilities` supports `postgres17`, `spark-sql` and `managed-notebooks`.
The inspector also infers required capabilities from resources. Other required
capabilities fail, including catalog/IAM features that are not implemented yet.
Unknown fields, shell hooks and configuration interpolation are rejected.

No targets means an implicit `local` development target. A sole target is selected
automatically; multiple targets require one declared `default = true` or an
explicit `--target`. Multiple defaults are always invalid. Development/production
modes describe intent and do not activate different security policies in PK01.

## File selection and bounds

`package.include` selects files. It supports literal relative paths, one `*` per
path segment and at most one standalone `**` directory segment per pattern.
A pattern matching no files fails. Resource files and dependency files must be
selected; declaring a reference alone does not silently add a file to the payload.
The root manifest and explicit resource fragments are always inventoried.

Top-level `include` names exact `resources/...toml` fragments, relative to the
project root. A fragment can contain `resources` and further `include` paths.
No override precedence exists: duplicate/cyclic fragments and resource keys fail.

The preview supports ASCII relative paths up to 512 bytes and 16 segments.
Traversal, links (including hard-linked files), special files, case-colliding
selected file/directory paths, private configuration/key paths and known cache/
venv directories fail. Wildcard traversal conservatively rejects symlinks in a
visited directory, even if that entry would not match the pattern. Avoid broad
whole-repository globs. A source edit or replacement detected during inspection
requires a retry; later apply must revalidate inputs, not trust an old report.

Limits: 256 KiB per manifest/resource fragment, 1 MiB per TOML/lock input, 8 MiB per
other file, 32 MiB total, 1,024 inventoried files, 4,096 directory entries visited,
256 resources, 32 environments, 32 targets, 128 payload patterns and 32 resource
fragments with nesting depth 4. Overlapping payload patterns deduplicate files;
identical patterns are rejected. Bounds do not establish a complete secret scan:
review arbitrary source/data before the later package export step.

The fixed-project MCP server exposes `project_inspect` and `project_validate` with
an optional `target`. These tools call the same offline API and cannot change the
session's project path or UUID. Runtime tools require the destination binding supplied by PK03.
Machine-readable schemas and the source example ship under `schemas/` and
`examples/projects/sales` in the release.

# Share a source project (PK02 preview)

A source package contains your declared code, notebooks, SQL, dependency declarations
and selected fixtures. It can be inspected and unpacked with only the Supabricks
binary. [PK03](project-deployments.md) adds explicit deployment binding; [PK04](project-apply.md) adds reviewed resource application;
source packages do not contain a prepared Python environment or database contents.

Start with a format-2 definition such as the shipped inspection example. See
[manifest fields and file selection](project-inspection.md). Keep running format-1
project manifests intact; conversion is not automatic.

```sh
supabricks project validate --project ./sales
supabricks project pack --project ./sales --output ./sales.sbproj
supabricks project inspect ./sales.sbproj
supabricks project verify ./sales.sbproj
supabricks project unpack ./sales.sbproj --destination ./sales-copy
```

Destinations must be new paths with existing parent directories. Existing files
or directories are never overwritten or merged. The JSON results show both
content and archive hashes, the complete inventory, exclusion policy, capabilities,
targets and unresolved database requirements. Review the inventory before sharing.
Use `--target NAME` to select a declared target; target modes grant no permissions.

Packing strips notebook outputs, execution counts, widgets and local runtime
metadata in the copy. It leaves the working files unchanged. Only declared files
are included. Private state, `.env`/key/config files, caches and virtual environments
cannot be selected. Recognized credential-bearing configuration URIs fail. Code,
SQL and arbitrary data may still contain secrets; these checks are not a complete
secret scan.

Unpack creates a private directory with `supabricks-unpacked.json` written last.
This receipt records the verified hashes and `unbound` state. It does not attach
the definition's UUID to a live database. Runtime commands reject an unbound copy until explicitly created or attached through PK03. Inspection and unpacking never
run SQL, Python, installation hooks or package resolvers.

## Export a saved query explicitly

Saved console queries live in private runtime storage and are not packaged by
walking your project directory. Get the query UUID/current revision from its saved
query response, then export that exact revision from the running format-1 project:

```sh
supabricks project export-query QUERY_UUID --expected-revision 3 \
  --project ./running-app --output ./sales/queries/example.sql
```

The CLI requires the existing daemon. It writes a new `.sql` file and preserves
the private original. A stale revision or another project's query fails. The
`saved_query_export` MCP tool returns the same portable SQL without writing a file.
Add that SQL path to `package.include` and declare its resource/database reference
in the format-2 definition. No private branch binding travels with the exported SQL.

## Limits and failure recovery

The source profile supports 1,024 files / 32 MiB total, 8 MiB per ordinary file,
1 MiB per TOML/lock, and 2 MiB of package metadata. Compressed and expanded archives
are bounded by a 300 MiB compressed/expanded envelope with a 200:1 expansion
bound. PK05 permits explicitly included `dependencies/*.zip` wheel bundles up to
128 MiB each / 256 MiB total, in addition to the 32 MiB source budget. See
[offline projects](project-offline.md) for target closure and initialization. PK01's smaller manifest, path,
graph and include limits also apply. USTAR imposes additional filename/prefix
limits; shorten paths if packaging reports they cannot be represented.

Malformed or truncated packages fail before any destination appears. Existing
destinations are preserved on error. A killed process can leave a private sibling
`.supabricks-package-*` directory; it is incomplete staging, not an installed
project. Remove it only after confirming no packaging operation is using it.

A verified hash proves integrity, not who authored the package or whether its code
is safe to execute. Offline dependency bundles, project apply, catalog integration
and access management are later slices.


## Browser workflow (PK06)

Open `supabricks console` from an existing local project, then choose **Project
packages**. Select a `.sbproj` file to verify its inventory and preview the
resource graph. **Unpack as new project** creates a new source directory; choose
**Create local deployment** or explicitly attach an existing deployment, then
**Plan deployment** and review the ordered steps before applying.

The source and deployment panel shows the worktree path for CLI use, public
and runtime identities, selected target, source hash and installed revision.
Apply errors retain the previous installed revision. If a reply is lost, use
**Recover apply by key**; refreshing/reopening also discovers the latest durable
operation. Correct the reported source, capability or preparation problem and
plan again. Database allocations and committed initialization are retained.

**Preview export** and **Download package** produce the same bytes as `project
pack`. Installed queries/notebooks can be viewed read-only or copied to a new
draft path without overwriting files. **Open console in new tab** preserves
existing query and notebook drafts; installed environment rows open the
corresponding environment worktree. Neither action starts a kernel.

Imported sources are retained under the cell's `console-projects` directory and
included in cold backups. Transfer slots are temporary (15-minute idle expiry,
300 MiB maximum); reselect the device file after an interrupted upload. See the
[console contract](../architecture/pk06-console-projects.md) for limits and
recovery behavior. Format-1 projects need explicit format-2 declarations and
`project adopt` before source export; the browser never infers those declarations.

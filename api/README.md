# Supabricks Platform API

The OpenAPI contract that the platform provides to every surface: the browser
console, the `supabricks` command line, MCP agents and customer automation.

It is a **design target**. It was derived from the console prototype
(`supabricks/console`, `prototype/`), where every screen was checked against
this repository. Each operation records which prototype features it serves and
whether the platform can already do it.

## Layout

```
api/
  openapi.yaml            Root: conventions, security, tags, and one line per path
  redocly.yaml            Lint rules
  shared/
    schemas.yaml          Id, Timestamp, Problem, Operation, PageInfo, references
    parameters.yaml       Path, paging, Idempotency-Key, If-Match
    responses.yaml        202 Accepted and the common errors
  domains/<domain>/
    paths.yaml            Path items for one domain, keyed by a short name
    schemas.yaml          Schemas for that domain
  scripts/coverage.py     Compares the spec with the prototype's feature list
```

`openapi.yaml` is the route index: every path appears there once, pointing at a
path item in a domain file. Domain files never reference each other's paths;
they share schemas only through `shared/`.

## Domains

| # | Domain | Scope | Prototype features | State |
|---|---|---|---|---|
| 1 | `projects` | Projects, start and stop, project map, services, recent items | HM, FR, ST-02 | Written |
| 2 | `operations` | Asynchronous work and the project activity feed | HM-03 | Written |
| 3 | `databases` | Database lifecycle, compute, connection details | DB-01 to DB-06, DB-11 | Written |
| 4 | `branches` | Branches, reset, restore window, schema diff | BR | Written |
| 5 | `database-admin` | PostgreSQL roles and privileges, password reset, extensions | DB-04c, DB-08, DB-09 | Planned |
| 6 | `database-observability` | Metric series, slow and running queries, locks | DB-07 | Planned |
| 7 | `backups` | Online backups, schedule, restore in place, restore history, timeline events | DB-10 | Planned |
| 8 | `sql` | Run statements on either engine, plans, saved queries, history | SQ, AN-01, AN-05 | Planned |
| 9 | `tables` | Paged rows with sort and filter, batched edits, reviewed DDL | TE | Planned |
| 10 | `objects` | Introspection: tables, columns, indexes, views, functions and the rest | OE, TE-04 | Planned |
| 11 | `imports` | Upload, inspect, mapping, load, history | IM | Planned |
| 12 | `sync` | Pipelines, runs, per-table state, lag history, budgets | SY | Planned |
| 13 | `analytics` | Published versions, pins, sessions and slots | AN | Planned |
| 14 | `notebooks` | Documents, kernels, execution, checkpoints, outputs | NB | Planned |
| 15 | `environments` | Python environment packages and revisions | NB (environment) | Planned |
| 16 | `jobs` | Jobs, triggers, runs, outputs and logs | JB | Planned |
| 17 | `catalog` | Tables, descriptions, lineage, publications, shared datasets | UC | Planned |
| 18 | `project-definition` | Resources, files, plan and apply, packages, data files | PJ | Planned |
| 19 | `access` | Project roles, data, run and catalog grants, effective access, project audit | AC-06 to AC-11 | Planned |
| 20 | `identity` | Sign-in, principals, groups, service accounts, API keys, provider, sessions, installation audit | AC-01 to AC-05, DB-05 | Planned |
| 21 | `secrets` | Secret store, grants, reads | SC | Planned |
| 22 | `alerts` | Alerts, rules, destinations, deliveries | AL | Planned |
| 23 | `usage` | Per-project and cross-project usage, limits | UQ | Planned |
| 24 | `server` | Version, verify, diagnostics, fixed limits | ST-01 | Planned |

## Conventions

The root file states these in full. In short:

- Paths address resources by opaque ID. Project-scoped paths start with
  `/projects/{project}`; installation-scoped paths (`identity`, `server`) do not.
- Work that takes time returns `202` and an `Operation`.
- Requests that create something or start work take `Idempotency-Key`.
- Reads return `ETag`; changes accept `If-Match`, and hard-to-undo changes
  require it.
- Lists take `limit` and `cursor`.
- Errors are `application/problem+json` with a stable `code`.
- Actions that are not a plain create, read, update or delete are a `POST` to a
  verb under the resource (`/suspend`, `/reset`, `/cancel`).

## Extension fields

Every operation carries:

| Field | Meaning |
|---|---|
| `x-backend` | `ready` (the platform does this today), `cli-only` (only from the command line or an admin request file), or `new` (no platform support) |
| `x-console-features` | Feature IDs from the prototype's `src/lib/backing.ts` that the operation serves |
| `x-issues` | Tracking issues on `supabricks/platform` for whatever part is not `ready` |

When one operation serves both a supported feature and an unsupported option
(for example creating a database, where choosing a compute size is new),
`x-backend` describes the basic call and the description says which part is not
available yet.

## Working on it

```sh
npx @redocly/cli lint api/openapi.yaml --config api/redocly.yaml
npx @redocly/cli bundle api/openapi.yaml -o openapi.bundle.yaml
python3 api/scripts/coverage.py ../console/prototype/src/lib/backing.ts
```

`coverage.py` lists, per prototype section, how many features have at least one
operation. A domain is finished when its sections show `done`.

To add a domain: create `domains/<name>/paths.yaml` and `schemas.yaml`, add its
paths and tag to `openapi.yaml`, and update the table above.

import { useEffect, useRef, useState } from "react";
import {
  workspace,
  type Branch,
  type Overview,
  type Target,
  type SqlResult,
  type QueryHandle,
  type Operation,
  type Saved,
  type WorkspaceCommand,
} from "./api";
const uuid = () => crypto.randomUUID();
const targetOf = (b: Branch): Target => ({
  branch: b.id,
  revision: b.revision,
});
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Workspace request failed.";
const pending = (h?: QueryHandle) =>
  h?.state === "running" || h?.state === "cancelling";
const quote = (s: string) => '"' + s.replaceAll('"', '""') + '"';
type Tab = {
  id: string;
  title: string;
  target: Target;
  branchName: string;
  sql: string;
  write: boolean;
  rows: number;
  timeout: number;
  handle?: QueryHandle;
  error?: string;
  submitting?: boolean;
  saved?: Saved;
  saveTitle: string;
};

function Results({ result }: { result: SqlResult }) {
  const [scroll, setScroll] = useState(0);
  const [copied, setCopied] = useState("");
  const start = Math.max(0, Math.floor(scroll / 34) - 4),
    end = Math.min(result.rows.length, start + 24);
  return (
    <div className="query-results">
      <div className="results-summary">
        {result.rows.length} rows · {result.affected_rows} affected{" "}
        <span role="status">{copied}</span>
      </div>
      {result.columns.length > 0 && (
        <div
          className="result-viewport"
          tabIndex={0}
          aria-label="Query results, scroll to view more rows"
          onScroll={(e) => setScroll(e.currentTarget.scrollTop)}
        >
          <table
            aria-label="SQL results"
            aria-rowcount={result.rows.length + 1}
          >
            <thead>
              <tr>
                {result.columns.map((c, i) => (
                  <th key={i} scope="col">
                    {c.name}
                    <small>{c.type}</small>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {start > 0 && (
                <tr aria-hidden="true">
                  <td
                    colSpan={result.columns.length}
                    className="spacer"
                    style={{ height: start * 34 }}
                  />
                </tr>
              )}
              {result.rows.slice(start, end).map((row, r) => (
                <tr key={start + r} aria-rowindex={start + r + 2}>
                  {row.map((v, c) => (
                    <td key={c}>
                      <button
                        className={`result-cell ${v === null ? "sql-null" : ""}`}
                        title={v === null ? "SQL NULL" : v}
                        aria-label={`${result.columns[c].name}, row ${start + r + 1}: ${v === null ? "SQL NULL" : v}`}
                        onClick={async () => {
                          if (v !== null) {
                            try {
                              await navigator.clipboard.writeText(v);
                              setCopied("Cell copied");
                            } catch {
                              setCopied("Copy unavailable");
                            }
                          }
                        }}
                      >
                        {v === null ? (
                          "NULL"
                        ) : v === "" ? (
                          <span className="muted">(empty string)</span>
                        ) : (
                          v
                        )}
                      </button>
                    </td>
                  ))}
                </tr>
              ))}
              {end < result.rows.length && (
                <tr aria-hidden="true">
                  <td
                    colSpan={result.columns.length}
                    className="spacer"
                    style={{ height: (result.rows.length - end) * 34 }}
                  />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function Workspace({
  data,
  selectedId,
  onSelect,
  onRefresh,
  visible,
}: {
  data: Overview;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onRefresh: () => Promise<void>;
  visible: boolean;
}) {
  const selected =
    data.branches.find((b) => b.id === selectedId) ??
    data.branches.find((b) => b.is_default) ??
    data.branches[0];
  const [tabs, setTabs] = useState<Tab[]>([]),
    [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState(""),
    [name, setName] = useState(""),
    [createKind, setCreateKind] = useState("branch");
  const [operation, setOperation] = useState<Operation | null>(null),
    [operationKey, setOperationKey] = useState("");
  const [mutating, setMutating] = useState(false),
    [deleteName, setDeleteName] = useState("");
  const [catalog, setCatalog] = useState<{
    target: Target;
    handle?: QueryHandle;
    error?: string;
  } | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]),
    [connection, setConnection] = useState<{
      branch: string;
      uri: string;
    } | null>(null);
  const [saving, setSaving] = useState(false),
    [notice, setNotice] = useState("");
  const alive = useRef(true),
    generation = useRef(data.runtime.generation),
    tabsRef = useRef(tabs);
  tabsRef.current = tabs;
  const editor = useRef<HTMLTextAreaElement>(null);
  const tab = tabs.find((t) => t.id === active);
  const current = tab && data.branches.find((b) => b.id === tab.target.branch);
  const stale =
    !!tab &&
    (!current || current.revision !== tab.target.revision || current.expired);
  const occupied = mutating || operation?.status === "pending";
  function patch(id: string, change: Partial<Tab>, handleId?: string) {
    if (alive.current)
      setTabs((ts) =>
        ts.map((t) =>
          t.id === id && (!handleId || t.handle?.id === handleId)
            ? { ...t, ...change }
            : t,
        ),
      );
  }
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  useEffect(() => {
    if (generation.current !== data.runtime.generation) {
      generation.current = data.runtime.generation;
      setTabs((ts) =>
        ts.map((t) => ({
          ...t,
          handle: undefined,
          submitting: false,
          write: false,
          error:
            "Runtime restarted. Previous write outcomes may be unknown; inspect before retrying.",
        })),
      );
      setCatalog(null);
      setConnection(null);
    }
  }, [data.runtime.generation]);
  useEffect(() => {
    setConnection(null);
    setCatalog(null);
    setDeleteName("");
  }, [selected?.id, selected?.revision]);
  async function listSaved() {
    try {
      const v = await workspace<{ queries: Saved[] }>({ action: "saved_list" });
      if (alive.current) setSaved(v.queries);
    } catch (e) {
      if (alive.current) setError(message(e));
    }
  }
  useEffect(() => {
    void listSaved();
  }, []);
  useEffect(() => {
    let stopped = false,
      working = false;
    const tick = async () => {
      if (stopped || working) return;
      working = true;
      try {
        for (const t of tabsRef.current.filter(
          (t) => pending(t.handle) && !t.submitting,
        )) {
          try {
            const h = await workspace<QueryHandle>({
              action: "query_status",
              id: t.handle!.id,
            });
            if (!stopped) patch(t.id, { handle: h }, t.handle!.id);
          } catch (e) {
            if (!stopped)
              patch(
                t.id,
                {
                  error: `${message(e)} Query outcome may be unknown. Check status or inspect the database; never replay automatically.`,
                  handle: { ...t.handle!, state: "failed" },
                },
                t.handle!.id,
              );
          }
        }
      } finally {
        working = false;
      }
    };
    const timer = setInterval(() => void tick(), 400);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!operation || operation.status !== "pending") return;
    let stopped = false,
      working = false;
    const timer = setInterval(async () => {
      if (working) return;
      working = true;
      try {
        const op = await workspace<Operation>({
          action: "operation",
          id: operation.id,
        });
        if (!stopped) {
          setOperation(op);
          if (op.status !== "pending") await onRefresh();
        }
      } catch (e) {
        if (!stopped)
          setError(
            `${message(e)} Inspect operation ${operation.id}; it was not resubmitted.`,
          );
      } finally {
        working = false;
      }
    }, 600);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [operation?.id, operation?.status]);
  useEffect(() => {
    if (!catalog?.handle || !pending(catalog.handle)) return;
    let stopped = false,
      working = false;
    const id = catalog.handle.id;
    const timer = setInterval(async () => {
      if (working) return;
      working = true;
      try {
        const handle = await workspace<QueryHandle>({
          action: "query_status",
          id,
        });
        if (!stopped)
          setCatalog((c) => (c?.handle?.id === id ? { ...c, handle } : c));
      } catch (e) {
        if (!stopped)
          setCatalog((c) =>
            c ? { ...c, error: message(e), handle: undefined } : c,
          );
      } finally {
        working = false;
      }
    }, 400);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [catalog?.handle?.id, catalog?.handle?.state]);
  async function mutate(command: WorkspaceCommand) {
    setError("");
    setMutating(true);
    setOperation(null);
    if ("key" in command) setOperationKey(command.key);
    try {
      const op = await workspace<Operation>(command);
      if (alive.current) {
        setOperation(op);
        setName("");
        setDeleteName("");
        await onRefresh();
      }
    } catch (e) {
      if (alive.current)
        setError(
          `${message(e)} The request was not retried. Refresh the branch inventory before issuing another operation.`,
        );
    } finally {
      if (alive.current) setMutating(false);
    }
  }
  function addTab(
    binding: Target,
    branchName: string,
    sql = "SELECT current_database();",
    title = "SQL query",
    savedQuery?: Saved,
  ) {
    if (tabsRef.current.length >= 8) {
      setError("Close a tab before opening another (eight-tab limit).");
      return null;
    }
    const t: Tab = {
      id: uuid(),
      title,
      target: binding,
      branchName,
      sql,
      write: false,
      rows: 200,
      timeout: 10000,
      saveTitle: savedQuery?.title ?? title,
      saved: savedQuery,
    };
    setTabs((ts) => [...ts, t]);
    setActive(t.id);
    setNotice("");
    requestAnimationFrame(() => editor.current?.focus());
    return t;
  }
  async function run(t: Tab, command?: WorkspaceCommand) {
    if (pending(t.handle) || t.submitting) return;
    const branch = data.branches.find((b) => b.id === t.target.branch);
    if (!branch || branch.revision !== t.target.revision || branch.expired) {
      patch(t.id, {
        error:
          "Branch changed or was deleted. Rebind explicitly before running.",
      });
      return;
    }
    const id = command && "id" in command ? command.id : uuid();
    const handle: QueryHandle = {
      id,
      generation: data.runtime.generation,
      target: t.target,
      state: "running",
      read_only: !t.write,
      elapsed_ms: 0,
    };
    patch(t.id, { error: "", submitting: true, handle });
    try {
      const h = await workspace<QueryHandle>(
        command ?? {
          action: "query",
          id,
          target: t.target,
          sql: t.sql,
          read_only: !t.write,
          max_rows: t.rows,
          timeout_ms: t.timeout,
        },
      );
      patch(t.id, { handle: h }, id);
    } catch (e) {
      patch(
        t.id,
        {
          handle: { ...handle, state: "failed" },
          error: `${message(e)} Query ${command && "id" in command ? command.id : id} was not replayed. If the response was lost, check its status before retrying a write.`,
        },
        id,
      );
    } finally {
      patch(t.id, { submitting: false }, id);
    }
  }
  async function loadCatalog() {
    if (!selected) return;
    const target = targetOf(selected),
      id = uuid();
    setCatalog({ target });
    try {
      const handle = await workspace<QueryHandle>({
        action: "catalog",
        id,
        target,
      });
      if (alive.current)
        setCatalog((c) =>
          c?.target.branch === target.branch ? { target, handle } : c,
        );
    } catch (e) {
      if (alive.current)
        setCatalog((c) =>
          c?.target.branch === target.branch
            ? { target, error: message(e) }
            : c,
        );
    }
  }
  async function preview(schema: string, table: string) {
    if (!selected) return;
    const target = targetOf(selected),
      id = uuid();
    const t = addTab(
      target,
      selected.name,
      `SELECT * FROM ${quote(schema)}.${quote(table)} LIMIT 200`,
      `${schema}.${table}`,
    );
    if (t) await run(t, { action: "preview", id, target, schema, table });
  }
  const tables = new Map<
    string,
    { schema: string; name: string; columns: (string | null)[][] }
  >();
  for (const row of catalog?.handle?.result?.rows ?? []) {
    const key = JSON.stringify(row.slice(0, 2));
    const table = tables.get(key) ?? {
      schema: row[0]!,
      name: row[1]!,
      columns: [],
    };
    table.columns.push(row);
    tables.set(key, table);
  }
  if (!data.capabilities.sql)
    return (
      <p>
        The runtime does not support the database workspace. Install a matching
        console release.
      </p>
    );
  return (
    <section
      className="database-workspace"
      hidden={!visible}
      aria-label="Database workspace"
    >
      <div className="page-heading">
        <div>
          <span className="eyebrow">POSTGRESQL WORKSPACE</span>
          <h1>Build on your data.</h1>
          <p>Explore a branch, write SQL, and keep experiments separate.</p>
        </div>
        <span className="local-pill">POSTGRESQL 17</span>
      </div>
      {error && (
        <div className="notice" role="alert">
          {error}
        </div>
      )}
      <div className="workspace-controls panel">
        <label>
          Navigation branch
          <select
            aria-label="Navigation branch"
            value={selected?.id ?? ""}
            onChange={(e) => onSelect(e.target.value)}
          >
            {!selected && <option value="">No databases yet</option>}
            {data.branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} · {b.desired_state}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button"
          disabled={!selected || occupied}
          onClick={() =>
            selected &&
            void mutate({
              action: "set_state",
              target: targetOf(selected),
              desired:
                selected.desired_state === "running" ? "suspended" : "running",
              key: uuid(),
            })
          }
        >
          {selected?.desired_state === "running"
            ? "Suspend branch"
            : "Resume branch"}
        </button>
        <button
          className="button primary"
          disabled={!selected || tabs.length >= 8}
          onClick={() => selected && addTab(targetOf(selected), selected.name)}
        >
          New SQL tab
        </button>
        <button
          className="button"
          disabled={!selected}
          onClick={async () => {
            if (!selected) return;
            try {
              const v = await workspace<{ uri: string }>({
                action: "connect",
                target: targetOf(selected),
              });
              if (alive.current)
                setConnection({ branch: selected.id, uri: v.uri });
            } catch (e) {
              setError(message(e));
            }
          }}
        >
          Reveal connection
        </button>
        {connection && connection.branch === selected?.id && (
          <div className="connection-reveal">
            <code>{connection.uri}</code>
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(connection.uri);
                  setNotice("Connection copied");
                } catch {
                  setNotice("Copy unavailable");
                }
              }}
            >
              Copy connection
            </button>
            <button onClick={() => setConnection(null)}>Hide connection</button>
          </div>
        )}
        <details className="branch-management">
          <summary>Create or delete a branch</summary>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void mutate(
                createKind === "database" || !selected
                  ? { action: "create_database", name, key: uuid() }
                  : {
                      action: "create_branch",
                      name,
                      target: targetOf(selected),
                      key: uuid(),
                    },
              );
            }}
          >
            <label>
              Create
              <select
                aria-label="Create kind"
                value={selected ? createKind : "database"}
                onChange={(e) => setCreateKind(e.target.value)}
              >
                <option value="database">Root database</option>
                {selected && (
                  <option value="branch">Child of {selected.name}</option>
                )}
              </select>
            </label>
            <label>
              Name
              <input
                aria-label="New branch name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={63}
              />
            </label>
            <button
              className="button"
              disabled={occupied || !name.trim()}
              type="submit"
            >
              Create
            </button>
          </form>
          {selected && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void mutate({
                  action: "delete_branch",
                  target: targetOf(selected),
                  key: uuid(),
                });
              }}
            >
              <label>
                Type {selected.name} to delete this branch
                <input
                  aria-label="Confirm branch deletion"
                  value={deleteName}
                  onChange={(e) => setDeleteName(e.target.value)}
                />
              </label>
              <button
                className="button danger"
                type="submit"
                disabled={occupied || deleteName !== selected.name}
              >
                Delete branch
              </button>
              <span className="muted">
                Active connections and child branches can prevent deletion.
              </span>
            </form>
          )}
        </details>
        {mutating && <p role="status">Submitting branch request…</p>}
        {operation && (
          <div className="operation-status" role="status">
            Operation {operation.id}:{" "}
            <strong>
              {operation.status === "pending"
                ? operation.next_step
                  ? "running"
                  : "accepted"
                : operation.status}
            </strong>{" "}
            · {operation.next_step}/{operation.steps.length} steps
            {!!operation.error && <pre>{JSON.stringify(operation.error)}</pre>}
          </div>
        )}
        {operationKey && (
          <span className="muted operation-key">
            Request key: {operationKey}
          </span>
        )}
      </div>
      <div className="database-columns">
        <aside className="explorer panel" aria-label="Data explorer">
          <div className="panel-heading">
            <h2>Tables</h2>
            <button
              disabled={
                !selected ||
                (!!catalog && !catalog.handle && !catalog.error) ||
                pending(catalog?.handle)
              }
              onClick={() => void loadCatalog()}
            >
              Refresh tables
            </button>
          </div>
          <p className="muted">
            {selected?.name ?? "Create a database to begin"} · up to 1,000
            columns
          </p>
          {catalog?.error && <p role="alert">{catalog.error}</p>}
          {catalog?.handle?.error && (
            <p role="alert">{catalog.handle.error.message}</p>
          )}
          {catalog &&
            ((!catalog.handle && !catalog.error) ||
              pending(catalog.handle)) && <p role="status">Loading catalog…</p>}
          {[...tables.values()].map((t) => (
            <details key={JSON.stringify([t.schema, t.name])}>
              <summary>
                {t.schema}.{t.name}
              </summary>
              <button onClick={() => void preview(t.schema, t.name)}>
                Preview {t.name}
              </button>
              <ul>
                {t.columns.map((c, i) => (
                  <li key={i}>
                    <strong>{c[2]}</strong>
                    <span>
                      {c[3]} {c[4] === "YES" ? "· nullable" : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          ))}
          {catalog?.handle?.state === "succeeded" && tables.size === 0 && (
            <p>No user tables in this branch.</p>
          )}
          <div className="panel-heading saved-heading">
            <h2>Saved queries</h2>
            <button onClick={() => void listSaved()}>
              Refresh saved queries
            </button>
          </div>
          {saved.length === 0 && (
            <p className="muted">
              Save a query explicitly to keep it on this device.
            </p>
          )}
          {saved.map((s) => (
            <div className="saved-entry" key={s.id}>
              <button
                onClick={async () => {
                  try {
                    const v = await workspace<Saved>({
                      action: "saved_get",
                      id: s.id,
                    });
                    if (alive.current)
                      addTab(
                        v.target,
                        data.branches.find((b) => b.id === v.target.branch)
                          ?.name ?? v.target.branch,
                        v.sql,
                        v.title,
                        v,
                      );
                  } catch (e) {
                    setError(message(e));
                  }
                }}
              >
                {s.title}
              </button>
              <button
                aria-label={`Delete saved query ${s.title}`}
                onClick={async () => {
                  try {
                    await workspace({
                      action: "saved_delete",
                      id: s.id,
                      expected_revision: s.revision,
                    });
                    await listSaved();
                  } catch (e) {
                    setError(message(e));
                  }
                }}
              >
                ×
              </button>
            </div>
          ))}
        </aside>
        <div className="sql-panel panel">
          <div className="sql-tabs" role="tablist" aria-label="SQL tabs">
            {tabs.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-label={`${t.title} · ${t.branchName}`}
                aria-selected={active === t.id}
                aria-controls="sql-editor-panel"
                onClick={() => setActive(t.id)}
              >
                {t.title} · {t.branchName}
                {pending(t.handle) ? " · running" : ""}
              </button>
            ))}
          </div>
          {!tab ? (
            <div className="sql-empty">
              <h2>Your next query starts here.</h2>
              <p>
                Choose a navigation branch and open a SQL tab. Tabs keep their
                own branch binding.
              </p>
            </div>
          ) : (
            <div
              id="sql-editor-panel"
              role="tabpanel"
              aria-label={`${tab.title} on ${tab.branchName}`}
            >
              <div className="query-binding">
                <strong>PostgreSQL · {tab.branchName}</strong>
                <code>{tab.target.branch}</code>
                <span>Revision {tab.target.revision}</span>
                <button
                  disabled={!selected || pending(tab.handle) || tab.submitting}
                  onClick={() =>
                    selected &&
                    patch(tab.id, {
                      target: targetOf(selected),
                      branchName: selected.name,
                      write: false,
                      handle: undefined,
                      error: "",
                    })
                  }
                >
                  Rebind to navigation branch
                </button>
                <button
                  disabled={pending(tab.handle) || tab.submitting}
                  onClick={() => {
                    setTabs((ts) => ts.filter((t) => t.id !== tab.id));
                    setActive(tabs.find((t) => t.id !== tab.id)?.id ?? null);
                  }}
                >
                  Close tab
                </button>
              </div>
              {stale && (
                <div className="notice" role="alert">
                  This tab’s branch changed or was deleted. Select a current
                  branch and explicitly rebind before running.
                </div>
              )}
              <label className="editor-label" htmlFor="sql-editor">
                SQL statement <span>One statement · Ctrl/⌘ Enter to run</span>
              </label>
              <textarea
                ref={editor}
                id="sql-editor"
                aria-label="SQL statement"
                spellCheck={false}
                value={tab.sql}
                maxLength={32768}
                onChange={(e) => patch(tab.id, { sql: e.target.value })}
                onKeyDown={(e) => {
                  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                    e.preventDefault();
                    if (!stale) void run(tab);
                  }
                }}
              />
              <div className="query-toolbar">
                <label className="write-toggle">
                  <input
                    type="checkbox"
                    checked={tab.write}
                    disabled={pending(tab.handle) || tab.submitting}
                    onChange={(e) => patch(tab.id, { write: e.target.checked })}
                  />
                  Allow writes to {tab.branchName}
                </label>
                <label>
                  Row limit
                  <input
                    aria-label="Row limit"
                    type="number"
                    min={1}
                    max={1000}
                    value={tab.rows}
                    onChange={(e) =>
                      patch(tab.id, { rows: Number(e.target.value) })
                    }
                  />
                </label>
                <label>
                  Timeout (ms)
                  <input
                    aria-label="Query timeout"
                    type="number"
                    min={100}
                    max={30000}
                    value={tab.timeout}
                    onChange={(e) =>
                      patch(tab.id, { timeout: Number(e.target.value) })
                    }
                  />
                </label>
                <button
                  className="button primary"
                  disabled={
                    stale ||
                    pending(tab.handle) ||
                    tab.submitting ||
                    !tab.sql.trim()
                  }
                  onClick={() => void run(tab)}
                >
                  Run SQL
                </button>
                <button
                  className="button"
                  disabled={!pending(tab.handle)}
                  onClick={async () => {
                    try {
                      patch(
                        tab.id,
                        {
                          handle: await workspace<QueryHandle>({
                            action: "cancel_query",
                            id: tab.handle!.id,
                          }),
                        },
                        tab.handle!.id,
                      );
                    } catch (e) {
                      patch(tab.id, { error: message(e) });
                    }
                  }}
                >
                  Cancel query
                </button>
              </div>
              <p className="query-limits">
                {tab.write
                  ? "Writes enabled for this tab. Interrupted writes may have committed; inspect before retrying."
                  : "Read-only transaction. Enable writes explicitly for database changes."}{" "}
                Results: at most 1,000 rows / 256 KiB; excess results fail.
              </p>
              <form
                className="save-query"
                onSubmit={async (e) => {
                  e.preventDefault();
                  setSaving(true);
                  try {
                    const s = await workspace<Saved>({
                      action: "saved_put",
                      id: tab.saved?.id ?? uuid(),
                      expected_revision: tab.saved?.revision ?? 0,
                      target: tab.target,
                      title: tab.saveTitle,
                      sql: tab.sql,
                    });
                    patch(tab.id, { saved: s, title: s.title });
                    setNotice("Query saved on this device");
                    await listSaved();
                  } catch (e) {
                    setError(message(e));
                  } finally {
                    setSaving(false);
                  }
                }}
              >
                <label>
                  Query title
                  <input
                    aria-label="Query title"
                    value={tab.saveTitle}
                    onChange={(e) =>
                      patch(tab.id, { saveTitle: e.target.value })
                    }
                    maxLength={120}
                    required
                  />
                </label>
                <button
                  disabled={stale || saving || !tab.sql.trim()}
                  type="submit"
                >
                  Save query
                </button>
                <span role="status">{notice}</span>
              </form>
              {tab.error && (
                <div className="notice" role="alert">
                  {tab.error}
                </div>
              )}
              {tab.handle && (
                <div className="query-status" role="status">
                  <strong>{tab.handle.state}</strong> · {tab.handle.elapsed_ms}{" "}
                  ms · {tab.handle.read_only ? "read only" : "writes enabled"}
                  <code>{tab.handle.id}</code>
                  <button
                    disabled={tab.submitting}
                    onClick={async () => {
                      try {
                        patch(
                          tab.id,
                          {
                            handle: await workspace<QueryHandle>({
                              action: "query_status",
                              id: tab.handle!.id,
                            }),
                            error: "",
                          },
                          tab.handle!.id,
                        );
                      } catch (e) {
                        patch(tab.id, { error: message(e) });
                      }
                    }}
                  >
                    Check query status
                  </button>
                </div>
              )}
              {tab.handle?.error && (
                <div className="notice" role="alert">
                  {tab.handle.error.message}
                </div>
              )}
              {tab.handle?.result && (
                <Results key={tab.handle.id} result={tab.handle.result} />
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

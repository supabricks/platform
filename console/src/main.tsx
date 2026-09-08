import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { authenticate, overview, logout, ApiError, type Overview } from "./api";
import "./style.css";

// A launch secret is single-use. Remove it before any API call or UI rendering.
const launch = new URLSearchParams(location.hash.slice(1)).get("launch");
history.replaceState(null, "", location.pathname);
const authentication = authenticate(launch);

function Mark() {
  return (
    <svg
      width="27"
      height="31"
      viewBox="0 0 27 31"
      fill="none"
      aria-hidden="true"
    >
      <path d="M13.5 1 26 8 13.5 15 1 8 13.5 1Z" fill="currentColor" />
      <path
        d="m1 15 12.5 7L26 15M1 22l12.5 7L26 22"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}
function App() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("");
  const [updated, setUpdated] = useState("");
  const [copied, setCopied] = useState(false);
  const [selection, setSelection] = useState<string | null>(null);
  const refreshGeneration = useRef(0);
  async function refresh() {
    const generation = ++refreshGeneration.current;
    setBusy(true);
    try {
      const result = await overview();
      if (generation !== refreshGeneration.current) return;
      setData(result);
      setError("");
      setUpdated(new Date().toLocaleTimeString());
    } catch (e) {
      if (generation !== refreshGeneration.current) return;
      setError(
        e instanceof Error ? e.message : "Unable to refresh the project.",
      );
      if (e instanceof ApiError && [401, 409].includes(e.status)) {
        setAuthenticated(false);
        setData(null);
      }
    } finally {
      if (generation === refreshGeneration.current) setBusy(false);
    }
  }
  useEffect(() => {
    let alive = true;
    authentication
      .then(() => {
        if (alive) {
          setAuthenticated(true);
          void refresh();
        }
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    const timer = setInterval(() => {
      if (!document.hidden) void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [authenticated]);
  const ready = data?.runtime.ready && !data.runtime.needs_attention && !error;
  const branches =
    data?.branches.filter((b) =>
      b.name.toLowerCase().includes(filter.toLowerCase()),
    ) ?? [];
  const selected = data?.branches.find((b) => b.id === selection);
  const shellQuote = (value: string) =>
    "'" + value.split("'").join("'\\''") + "'";
  const createCommand = data
    ? `supabricks database create main --wait --project ${shellQuote(data.worktree)} --data-dir ${shellQuote(data.data_dir)}`
    : "";
  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to overview
      </a>
      <aside className="sidebar">
        <div className="brand">
          <Mark />
          <span>
            supabricks<span className="brand-dot">.</span>
          </span>
        </div>
        <span className="eyebrow workspace-label">YOUR WORKSPACE</span>
        <div className="project-card">
          <span className="project-monogram">
            {data?.project.name[0]?.toUpperCase() ?? "S"}
          </span>
          <div>
            <strong>{data?.project.name ?? "Local project"}</strong>
            <span>On this device</span>
          </div>
        </div>
        <nav aria-label="Project navigation">
          <a href="#main" aria-current="page">
            <span aria-hidden="true">▦</span> Overview
          </a>
        </nav>
        <div className="sidebar-bottom">
          <span className="local-pill">
            <i /> LOCAL CONSOLE
          </span>
          <p>
            Your database runs here.
            <br />
            Your data stays here.
          </p>
          <span className="subtle">Apache 2.0 · Supabricks</span>
        </div>
      </aside>
      <div className="workspace">
        <header>
          <div className="crumb">
            Workspace <span>/</span>{" "}
            <strong>{data?.project.name ?? "Overview"}</strong>
          </div>
          <div className="header-actions">
            <span className={`runtime-pill ${ready ? "ready" : ""}`}>
              <i />
              {ready
                ? "Runtime ready"
                : error
                  ? "Connection needs attention"
                  : "Connecting"}
            </span>
            {authenticated && (
              <button
                className="text-button"
                onClick={async () => {
                  try {
                    await logout();
                    ++refreshGeneration.current;
                    setAuthenticated(false);
                    setData(null);
                    setError(
                      "Session closed. Run supabricks console to open a new session.",
                    );
                  } catch (e) {
                    setError(
                      e instanceof Error
                        ? e.message
                        : "Unable to close session.",
                    );
                  }
                }}
              >
                Sign out
              </button>
            )}
          </div>
        </header>
        <main id="main">
          <div className="page-heading">
            <div>
              <span className="eyebrow">PROJECT OVERVIEW</span>
              <h1>A home for your local data.</h1>
              <p>Your branches and runtime, together in one place.</p>
            </div>
            {authenticated && (
              <button
                className="button"
                disabled={busy}
                onClick={() => void refresh()}
              >
                {busy ? "Refreshing…" : "↻  Refresh"}
              </button>
            )}
          </div>
          {error && (
            <div className="notice" role="alert">
              <strong>Let’s reconnect.</strong>
              <p>{error}</p>
              <code>supabricks console</code>
            </div>
          )}
          {!data && !error && (
            <div className="loading" role="status">
              Connecting to your local project…
            </div>
          )}
          {data && (
            <>
              <section className="stats" aria-label="Project summary">
                <article>
                  <span className="eyebrow">BRANCHES</span>
                  <div className="stat-value">
                    {data.branches.length.toString().padStart(2, "0")}
                    <span className="stat-icon" aria-hidden="true">
                      ⑂
                    </span>
                  </div>
                  <p>Independent places to build</p>
                </article>
                <article>
                  <span className="eyebrow">DATABASE ENGINE</span>
                  <div className="stat-value engine-value">
                    Postgres <span>{data.runtime.postgres_major}</span>
                  </div>
                  <p>Native on your machine</p>
                </article>
                <article>
                  <span className="eyebrow">LOCAL RUNTIME</span>
                  <div
                    className={`stat-value runtime-value ${ready ? "healthy" : ""}`}
                  >
                    <i />
                    {ready ? "Ready" : error ? "Unavailable" : "Starting"}
                  </div>
                  <p>
                    {ready
                      ? "Available for your applications"
                      : "Run supabricks doctor for details"}
                  </p>
                </article>
              </section>
              <section className="branch-panel" aria-labelledby="branch-title">
                <div className="panel-heading">
                  <div>
                    <h2 id="branch-title">
                      Branches <span>{data.branches.length}</span>
                    </h2>
                    <p>Each branch is a separate version of your database.</p>
                  </div>
                  {data.branches.length > 0 && (
                    <label className="search">
                      <span className="sr-only">Find a branch</span>
                      <input
                        placeholder="Find a branch…"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                      />
                    </label>
                  )}
                </div>
                {data.branches.length === 0 ? (
                  <div className="empty">
                    <span className="empty-mark" aria-hidden="true">
                      ⑂
                    </span>
                    <h3>Your first branch starts here.</h3>
                    <p>
                      Create a database in this project, then refresh to see it
                      here.
                    </p>
                    <div className="command">
                      <code>{createCommand}</code>
                      <button
                        aria-label="Copy create database command"
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(createCommand);
                            setCopied(true);
                          } catch {
                            setCopied(false);
                          }
                        }}
                      >
                        {copied ? "Copied" : "Copy"}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Branch</th>
                          <th>Parent</th>
                          <th>Desired state</th>
                          <th>Revision</th>
                          <th>Identity</th>
                        </tr>
                      </thead>
                      <tbody>
                        {branches.map((b) => (
                          <tr
                            key={b.id}
                            className={selection === b.id ? "selected" : ""}
                          >
                            <td>
                              <button
                                className="branch-name"
                                onClick={() =>
                                  setSelection(selection === b.id ? null : b.id)
                                }
                                aria-expanded={selection === b.id}
                              >
                                <span aria-hidden="true">⑂</span>
                                {b.name}
                              </button>
                              {b.is_default && (
                                <span className="default-tag">Default</span>
                              )}
                            </td>
                            <td>
                              {b.parent_id ? (
                                (data.branches.find((p) => p.id === b.parent_id)
                                  ?.name ?? b.parent_id.slice(0, 8))
                              ) : (
                                <span className="muted">Root database</span>
                              )}
                            </td>
                            <td>
                              <span
                                className={`state ${b.desired_state === "running" ? "running" : ""}`}
                              >
                                <i />
                                {b.expired ? "Expired" : b.desired_state}
                              </span>
                            </td>
                            <td className="mono">{b.revision}</td>
                            <td className="mono muted">{b.id.slice(0, 8)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {branches.length === 0 && (
                      <p className="no-match">No branches match “{filter}”.</p>
                    )}
                  </div>
                )}
                {selected && (
                  <div className="branch-detail">
                    <strong>{selected.name}</strong>
                    <code>{selected.id}</code>
                    <span>Selection is local to this console.</span>
                  </div>
                )}
                <div className="panel-footer">
                  <span>
                    <i className="live-dot" />
                    {error
                      ? "Showing last received state"
                      : "Connected to local runtime"}
                  </span>
                  <span>Updated {updated}</span>
                </div>
              </section>
              <section
                className="project-details"
                aria-label="Project location"
              >
                <div>
                  <span className="eyebrow">PROJECT DIRECTORY</span>
                  <code>{data.worktree}</code>
                </div>
                <div>
                  <span className="eyebrow">PROJECT ID</span>
                  <code>{data.project.id}</code>
                </div>
              </section>
            </>
          )}
          <footer>
            Built for the work in front of you.
            <span>Supabricks local preview</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);

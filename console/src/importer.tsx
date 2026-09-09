import { useEffect, useRef, useState } from "react";
import {
  ingest,
  upload,
  type Overview,
  type Branch,
  type Mapping,
  type ImportSource,
  type SourceStatus,
  type ImportJob,
} from "./api";
const options = (): Mapping => ({
  version: 1,
  format: "csv",
  delimiter: ",",
  header: true,
  null_strings: [],
  columns: [
    {
      input: "0",
      name: "column_1",
      data_type: { kind: "text" },
      nullable: true,
    },
  ],
});
const active = (j: ImportJob) =>
  ["queued", "loading", "reconciling"].includes(j.state);
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Import request failed";
const types = [
  "text",
  "boolean",
  "smallint",
  "integer",
  "bigint",
  "decimal",
  "double",
  "date",
  "timestamp",
  "timestamp_tz",
];
export function Importer({
  data,
  selected,
  onOpen,
}: {
  data: Overview;
  selected?: Branch;
  onOpen: (job: ImportJob) => void;
}) {
  const [open, setOpen] = useState(false),
    [source, setSource] = useState<SourceStatus | null>(null);
  const [sources, setSources] = useState<ImportSource[]>([]),
    [jobs, setJobs] = useState<ImportJob[]>([]);
  const [mapping, setMapping] = useState<Mapping>(options),
    [nulls, setNulls] = useState("[]");
  const [target, setTarget] = useState<Branch | undefined>(selected),
    [schema, setSchema] = useState("public"),
    [table, setTable] = useState("");
  const [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [bytes, setBytes] = useState(0),
    [total, setTotal] = useState(0),
    [approved, setApproved] = useState(false);
  const [dirty, setDirty] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const controller = useRef<AbortController | null>(null),
    sourceId = useRef<string | null>(null),
    alive = useRef(true),
    opened = useRef(new Set<string>()),
    submitted = useRef<string | null>(null);
  const sourceKey = `supabricks.import.source.${data.project.id}`,
    pendingKey = `supabricks.import.pending.${data.project.id}`;
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  async function refresh() {
    const [list, slots] = await Promise.all([
      ingest<ImportJob[]>({ action: "list" }),
      ingest<{ source: ImportSource }[]>({ action: "sources" }),
    ]);
    if (!alive.current) return;
    setJobs(list);
    setSources(slots.map((s) => s.source));
    if (
      sourceId.current &&
      !slots.some((s) => s.source.id === sourceId.current)
    ) {
      sourceId.current = null;
      setSource(null);
      sessionStorage.removeItem(sourceKey);
    }
    if (sourceId.current) {
      try {
        const status = await ingest<SourceStatus>({
          action: "source",
          source: sourceId.current,
        });
        if (alive.current && status.status.source.id === sourceId.current)
          setSource(status);
      } catch (e) {
        if (alive.current) {
          setError(message(e));
          sourceId.current = null;
          setSource(null);
          sessionStorage.removeItem(sourceKey);
        }
      }
    }
    const found = list.find((j) => j.id === submitted.current);
    if (found?.state === "succeeded" && !opened.current.has(found.id)) {
      opened.current.add(found.id);
      onOpenRef.current(found);
    }
  }
  useEffect(() => {
    alive.current = true;
    sourceId.current = sessionStorage.getItem(sourceKey);
    setUncertain(!!sessionStorage.getItem(pendingKey));
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try {
        await refresh();
      } catch (e) {
        if (alive.current) setError(message(e));
      } finally {
        running = false;
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), 1000);
    return () => {
      alive.current = false;
      clearInterval(timer);
      controller.current?.abort();
    };
  }, [data.project.id]);
  useEffect(() => {
    if (source?.status.inspection) {
      setMapping(source.status.inspection.mapping);
      setNulls(JSON.stringify(source.status.inspection.mapping.null_strings));
      setDirty(false);
      setApproved(false);
    }
  }, [source?.status.source.id, source?.preview, !!source?.status.inspection]);
  function change(next: Mapping, parser = false) {
    setMapping(next);
    setApproved(false);
    if (parser) setDirty(true);
  }
  async function choose(file?: File) {
    if (!file || busy) return;
    setError("");
    setApproved(false);
    setUncertain(false);
    sessionStorage.removeItem(pendingKey);
    if (!file.size || file.size > 100 * 1024 * 1024) {
      setError("Select a nonempty CSV or TSV file up to 100 MiB.");
      return;
    }
    setBusy("Uploading");
    setBytes(0);
    setTotal(file.size);
    setTarget(selected);
    setTable(
      file.name
        .replace(/\.[^.]*$/, "")
        .replace(/[^a-zA-Z0-9_]/g, "_")
        .slice(0, 50) || "imported_data",
    );
    const abort = new AbortController();
    controller.current = abort;
    try {
      const slot = await ingest<{ source: ImportSource }>({
        action: "begin",
        name: file.name,
        bytes: file.size,
      });
      sourceId.current = slot.source.id;
      sessionStorage.setItem(sourceKey, slot.source.id);
      setSource(null);
      await upload(slot.source.id, file, setBytes, abort.signal);
      setBusy("Inspecting");
      const parser = {
        ...mapping,
        delimiter: /\.tsv$/i.test(file.name) ? "\t" : mapping.delimiter,
      };
      await ingest({
        action: "inspect",
        source: slot.source.id,
        mapping: parser,
      });
      await refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      if (alive.current) setBusy("");
      controller.current = null;
    }
  }
  async function act(command: object) {
    setError("");
    setBusy("Submitting");
    try {
      await ingest(command);
      await refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy("");
    }
  }
  const staged = source?.status.source,
    inspection = source?.status.inspection;
  const stale =
    !target ||
    !data.branches.some(
      (b) =>
        b.id === target.id &&
        b.revision === target.revision &&
        !b.expired &&
        b.desired_state === "running",
    );
  async function load() {
    if (!staged?.sha256 || !source || !target || dirty) return;
    const key = crypto.randomUUID();
    sessionStorage.setItem(pendingKey, key);
    setUncertain(true);
    setBusy("Accepting import");
    setError("");
    try {
      const j = await ingest<ImportJob>({
        action: "load",
        key,
        preview: source.preview,
        load: {
          version: 1,
          project_id: data.project.id,
          branch_id: target.id,
          branch_revision: target.revision,
          source_id: staged.id,
          source_sha256: staged.sha256,
          mapping,
          schema,
          table,
        },
      });
      submitted.current = j.id;
      sessionStorage.removeItem(pendingKey);
      setUncertain(false);
      setApproved(false);
      await refresh();
    } catch (e) {
      setError(
        `${message(e)}. Inspect recent imports before submitting another request.`,
      );
    } finally {
      setBusy("");
    }
  }
  return (
    <section className="importer panel" aria-label="File importer">
      <div className="panel-heading">
        <div>
          <h2>Bring your data.</h2>
          <p className="muted">
            CSV & TSV · up to 100 MiB · a new PostgreSQL table
          </p>
        </div>
        <button
          className="button primary"
          onClick={() => {
            setOpen(!open);
            if (!target) setTarget(selected);
          }}
        >
          {open ? "Hide importer" : "Import file"}
        </button>
      </div>
      {open && (
        <>
          {error && (
            <div className="notice" role="alert">
              {error}
            </div>
          )}
          <div
            className="import-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              void choose(e.dataTransfer.files[0]);
            }}
          >
            <label>
              Choose CSV or TSV
              <input
                aria-label="Choose CSV or TSV"
                type="file"
                accept=".csv,.tsv,text/csv,text/tab-separated-values"
                disabled={!!busy}
                onChange={(e) => {
                  void choose(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </label>
            <span className="muted">
              Or drop one file here. Your original file stays on your device.
            </span>
          </div>
          {busy && (
            <p role="status">
              {busy}
              {busy === "Uploading" &&
                ` · ${bytes.toLocaleString()} / ${total.toLocaleString()} bytes sent`}
              …{" "}
              <button
                onClick={() => controller.current?.abort()}
                disabled={!controller.current}
              >
                Cancel upload
              </button>
            </p>
          )}
          {sources.length > 0 && (
            <label>
              Staged files
              <select
                aria-label="Staged files"
                value={staged?.id ?? ""}
                onChange={(e) => {
                  sourceId.current = e.target.value;
                  sessionStorage.setItem(sourceKey, e.target.value);
                  setSource(null);
                  void refresh();
                }}
              >
                <option value="" disabled>
                  Select a staged file
                </option>
                {sources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.display_name} · {s.state}
                  </option>
                ))}
              </select>
            </label>
          )}
          {staged && (
            <div className="import-source">
              <strong>{staged.display_name}</strong> · {staged.state} · retained
              until {new Date(staged.expires_at_ms).toLocaleString()}{" "}
              <button
                disabled={!!busy}
                onClick={() =>
                  void act({ action: "dispose", source: staged.id })
                }
              >
                Dispose source
              </button>
            </div>
          )}
          <fieldset disabled={!!busy}>
            <legend>Parser options</legend>
            <div className="import-options">
              <label>
                Delimiter
                <select
                  aria-label="Delimiter"
                  value={mapping.delimiter}
                  onChange={(e) =>
                    change({ ...mapping, delimiter: e.target.value }, true)
                  }
                >
                  <option value=",">Comma</option>
                  <option value={"\t"}>Tab</option>
                  <option value=";">Semicolon</option>
                  <option value="|">Pipe</option>
                </select>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={mapping.header}
                  onChange={(e) =>
                    change({ ...mapping, header: e.target.checked }, true)
                  }
                />
                First row is a header
              </label>
              <label>
                Null strings (JSON array)
                <input
                  aria-label="Null strings"
                  value={nulls}
                  maxLength={4096}
                  onChange={(e) => {
                    setNulls(e.target.value);
                    setDirty(true);
                    setApproved(false);
                  }}
                />
              </label>
              <button
                disabled={!staged || staged.state !== "staged"}
                onClick={() => {
                  try {
                    const values: unknown = JSON.parse(nulls);
                    if (
                      !Array.isArray(values) ||
                      !values.every((v) => typeof v === "string")
                    )
                      throw new Error("Enter an array of strings");
                    void act({
                      action: "inspect",
                      source: staged!.id,
                      mapping: { ...mapping, null_strings: values },
                    });
                  } catch (e) {
                    setError(message(e));
                  }
                }}
              >
                Inspect again
              </button>
            </div>
          </fieldset>
          {staged?.state === "receiving" && !busy && (
            <p role="status">Inspecting uploaded bytes…</p>
          )}
          {source?.status.error && (
            <div className="notice" role="alert">
              Inspection failed: {source.status.error}. Dispose this source or
              inspect it again.
            </div>
          )}
          {inspection && (
            <>
              <h3>Review columns</h3>
              <p className="muted">
                Text preserves the original values. Choose conversions
                explicitly. The sample is limited to 100 rows; every row is
                checked during import.
              </p>
              <div className="import-grid">
                <table aria-label="Column mapping">
                  <thead>
                    <tr>
                      <th>Input</th>
                      <th>Column name</th>
                      <th>PostgreSQL type</th>
                      <th>Nullable</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mapping.columns.map((c, i) => (
                      <tr key={i}>
                        <td>{Number(c.input) + 1}</td>
                        <td>
                          <input
                            aria-label={`Column ${i + 1} name`}
                            value={c.name}
                            maxLength={63}
                            onChange={(e) =>
                              change({
                                ...mapping,
                                columns: mapping.columns.map((v, n) =>
                                  n === i ? { ...v, name: e.target.value } : v,
                                ),
                              })
                            }
                          />
                        </td>
                        <td>
                          <select
                            aria-label={`Column ${i + 1} type`}
                            value={c.data_type.kind}
                            onChange={(e) =>
                              change({
                                ...mapping,
                                columns: mapping.columns.map((v, n) =>
                                  n === i
                                    ? {
                                        ...v,
                                        data_type:
                                          e.target.value === "decimal"
                                            ? {
                                                kind: "decimal",
                                                precision: 18,
                                                scale: 2,
                                              }
                                            : { kind: e.target.value },
                                      }
                                    : v,
                                ),
                              })
                            }
                          >
                            {types.map((t) => (
                              <option key={t}>{t}</option>
                            ))}
                          </select>
                          {c.data_type.kind === "decimal" && (
                            <>
                              <input
                                aria-label={`Column ${i + 1} precision`}
                                type="number"
                                min={1}
                                max={38}
                                value={c.data_type.precision}
                                onChange={(e) =>
                                  change({
                                    ...mapping,
                                    columns: mapping.columns.map((v, n) =>
                                      n === i
                                        ? {
                                            ...v,
                                            data_type: {
                                              ...v.data_type,
                                              precision: Number(e.target.value),
                                            },
                                          }
                                        : v,
                                    ),
                                  })
                                }
                              />
                              <input
                                aria-label={`Column ${i + 1} scale`}
                                type="number"
                                min={0}
                                max={38}
                                value={c.data_type.scale}
                                onChange={(e) =>
                                  change({
                                    ...mapping,
                                    columns: mapping.columns.map((v, n) =>
                                      n === i
                                        ? {
                                            ...v,
                                            data_type: {
                                              ...v.data_type,
                                              scale: Number(e.target.value),
                                            },
                                          }
                                        : v,
                                    ),
                                  })
                                }
                              />
                            </>
                          )}
                        </td>
                        <td>
                          <input
                            aria-label={`Column ${i + 1} nullable`}
                            type="checkbox"
                            checked={c.nullable}
                            onChange={(e) =>
                              change({
                                ...mapping,
                                columns: mapping.columns.map((v, n) =>
                                  n === i
                                    ? { ...v, nullable: e.target.checked }
                                    : v,
                                ),
                              })
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <details>
                <summary>Preview {inspection.rows.length} sample rows</summary>
                <div className="import-grid">
                  <table aria-label="Import sample">
                    <thead>
                      <tr>
                        {mapping.columns.map((c, i) => (
                          <th key={i}>{c.name}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {inspection.rows.map((row, r) => (
                        <tr key={r}>
                          {row.map((v, c) => (
                            <td key={c}>
                              {v === null ? (
                                <em>NULL</em>
                              ) : v === "" ? (
                                <em>empty string</em>
                              ) : (
                                v
                              )}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <fieldset disabled={!!busy}>
                <legend>Create a new table</legend>
                <p>
                  Project: <strong>{data.project.name}</strong>{" "}
                  <code>{data.project.id}</code>
                </p>
                <div className="import-options">
                  <label>
                    Import branch
                    <select
                      aria-label="Import branch"
                      value={target?.id ?? ""}
                      onChange={(e) => {
                        setTarget(
                          data.branches.find((b) => b.id === e.target.value),
                        );
                        setApproved(false);
                      }}
                    >
                      <option value="" disabled>
                        Select a branch
                      </option>
                      {data.branches.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name} · revision {b.revision}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Schema
                    <input
                      aria-label="Import schema"
                      value={schema}
                      maxLength={63}
                      onChange={(e) => {
                        setSchema(e.target.value);
                        setApproved(false);
                      }}
                    />
                  </label>
                  <label>
                    New table name
                    <input
                      aria-label="New table name"
                      value={table}
                      maxLength={63}
                      onChange={(e) => {
                        setTable(e.target.value);
                        setApproved(false);
                      }}
                    />
                  </label>
                </div>
                <p>
                  <strong>
                    {target?.name} → {schema}.{table}
                  </strong>{" "}
                  · revision {target?.revision}
                </p>
                <p className="muted import-hash">
                  Content SHA-256: {staged?.sha256}
                </p>
                {stale && (
                  <p role="alert">
                    Select a current running branch before importing.
                  </p>
                )}
                {dirty && (
                  <p role="alert">
                    Parser options changed. Inspect again before approval.
                  </p>
                )}
                <label className="import-approval">
                  <input
                    type="checkbox"
                    checked={approved}
                    onChange={(e) => setApproved(e.target.checked)}
                    disabled={dirty || stale}
                  />
                  I approve these columns and this destination.
                </label>
                <button
                  className="button primary"
                  disabled={
                    !approved ||
                    dirty ||
                    stale ||
                    !schema ||
                    !table ||
                    uncertain ||
                    jobs.some(active) ||
                    staged?.state !== "staged"
                  }
                  onClick={() => void load()}
                >
                  Create table and import
                </button>
                {uncertain && (
                  <p role="alert">
                    A submission needs review. Check recent imports before
                    choosing a file for another import.
                  </p>
                )}
              </fieldset>
            </>
          )}
        </>
      )}
      {jobs.length > 0 && (
        <div className="import-jobs">
          <h3>Recent imports</h3>
          {jobs.map((j) => (
            <article key={j.id} className="import-job">
              <div>
                <strong>
                  {data.branches.find((b) => b.id === j.load.branch_id)?.name ??
                    j.load.branch_id}{" "}
                  · {j.load.schema}.{j.load.table}
                </strong>
                <span role="status">
                  {" "}
                  {j.state} · attempt {j.attempt}
                </span>
              </div>
              <p>
                Parsed {j.parsed_rows.toLocaleString()} · Copied{" "}
                {j.copied_rows.toLocaleString()} ·{" "}
                <strong>
                  {j.committed_rows === null
                    ? "Awaiting commit confirmation"
                    : `${j.committed_rows.toLocaleString()} committed rows`}
                </strong>
              </p>
              <small>{j.id}</small>
              <div>
                {active(j) && (
                  <button
                    disabled={!!busy}
                    onClick={() => void act({ action: "cancel", id: j.id })}
                  >
                    Cancel import
                  </button>
                )}
                {j.state === "failed" && j.retryable && !j.source_released && (
                  <button
                    disabled={!!busy || jobs.some(active)}
                    onClick={() => void act({ action: "retry", id: j.id })}
                  >
                    Retry retained source
                  </button>
                )}
                {j.state === "succeeded" && (
                  <button onClick={() => onOpen(j)}>Open {j.load.table}</button>
                )}
                <button
                  onClick={async () => {
                    try {
                      const status = await ingest<ImportJob>({
                        action: "status",
                        id: j.id,
                      });
                      setError(
                        status.error
                          ? `Import detail: ${status.error}`
                          : `Import ${status.state}`,
                      );
                    } catch (e) {
                      setError(message(e));
                    }
                    setOpen(true);
                  }}
                >
                  Import details
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

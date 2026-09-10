import { useEffect, useRef, useState } from "react";
import {
  notebookList,
  notebookGet,
  notebookSave,
  notebookContexts,
  notebookLifecycle,
  notebookRename,
  notebookRefresh,
  type NotebookRefresh,
  type NotebookContext,
  type Overview,
} from "./api";
import { DocumentEditor, newDocument } from "./notebooks/editor";
import { NotebookChannel } from "./notebooks/channel";

const live = (c: NotebookContext | null) =>
  !!c &&
  ["starting", "ready", "busy", "interrupting", "stopping"].includes(c.state);
const available = (c: NotebookContext) =>
  ["ready", "busy", "interrupting"].includes(c.state);
type Provenance = { branch_id: string; epoch_id: string | null };

export function Notebook({
  visible,
  data,
}: {
  visible: boolean;
  data: Overview;
}) {
  const host = useRef<HTMLDivElement>(null),
    editor = useRef<DocumentEditor>(null),
    channel = useRef<NotebookChannel>(null);
  const abort = useRef(new AbortController()),
    lock = useRef(false),
    cancelRun = useRef(false),
    dirtyRef = useRef(false);
  const current = useRef<NotebookContext | null>(null);
  const [files, setFiles] = useState<string[]>([]),
    [path, setPath] = useState(""),
    [revision, setRevision] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [running, setRunning] = useState(false),
    [connected, setConnected] = useState(false);
  const [message, setMessage] = useState(
      "Choose a branch, then start a kernel.",
    ),
    [error, setError] = useState("");
  const [branch, setBranch] = useState(""),
    [context, setContext] = useState<NotebookContext | null>(null),
    [contexts, setContexts] = useState<NotebookContext[]>([]);
  const [persistOutputs, setPersistOutputs] = useState(false),
    [provenance, setProvenance] = useState<Provenance | null>(null);
  const [snapshotRefresh, setSnapshotRefresh] =
    useState<NotebookRefresh | null>(null);
  const refreshing =
    !!snapshotRefresh &&
    !["published", "failed", "cancelled"].includes(snapshotRefresh.state);
  function update(c: NotebookContext | null) {
    current.current = c;
    setContext(c);
  }
  function markDirty(value: boolean) {
    dirtyRef.current = value;
    setDirty(value);
  }
  function disconnect() {
    channel.current?.dispose();
    channel.current = null;
    setConnected(false);
  }
  async function perform(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      if (!abort.current.signal.aborted)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      lock.current = false;
      if (!abort.current.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    abort.current = controller;
    const instance = new DocumentEditor(host.current!, () => markDirty(true));
    editor.current = instance;
    const unload = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", unload);
    return () => {
      controller.abort();
      channel.current?.dispose();
      instance.dispose();
      window.removeEventListener("beforeunload", unload);
    };
  }, []);
  useEffect(() => {
    if (!visible) return;
    void perform(async () => {
      setFiles(await notebookList());
      setContexts(await notebookContexts());
    });
    editor.current?.widget.update();
  }, [visible]);
  useEffect(() => {
    let checking = false;
    const timer = setInterval(async () => {
      if (checking || lock.current || !current.current) return;
      checking = true;
      const requested = current.current;
      try {
        const entries = await notebookContexts();
        if (
          abort.current.signal.aborted ||
          lock.current ||
          current.current !== requested
        )
          return;
        setContexts(entries);
        const next = entries.find((c) => c.id === current.current?.id);
        if (!next) {
          disconnect();
          update(null);
          setError("Kernel context was lost. Start a new kernel explicitly.");
        } else {
          if (
            next.generation !== current.current?.generation ||
            !available(next)
          ) {
            disconnect();
          }
          update(next);
          if (next.error) setError(`Kernel ${next.state}: ${next.error}`);
        }
      } catch (e) {
        if (!abort.current.signal.aborted) {
          disconnect();
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        checking = false;
      }
    }, 2000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!snapshotRefresh || !refreshing) return;
    let alive = true;
    let checking = false;
    const timer = setInterval(async () => {
      if (checking) return;
      checking = true;
      try {
        const result = await notebookRefresh({
          action: "notebook_refresh_status",
          id: snapshotRefresh.id,
        });
        if (alive) {
          setSnapshotRefresh(result);
          if (result.error) setError(result.error);
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        checking = false;
      }
    }, 1000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [snapshotRefresh?.id, refreshing]);
  function discard() {
    return (
      !dirtyRef.current ||
      window.confirm(
        "Discard unsaved notebook changes? Download or save first to keep them.",
      )
    );
  }
  function reset(nextPath = "", nextRevision: string | null = null) {
    setPath(nextPath);
    setRevision(nextRevision);
    markDirty(false);
    setPersistOutputs(false);
    setError("");
  }
  async function open(file: string) {
    if (!discard()) return;
    const instance = editor.current!;
    instance.model.readOnly = true;
    try {
      const loaded = await notebookGet(file);
      abort.current.signal.throwIfAborted();
      disconnect();
      update(null);
      instance.load(loaded.document);
      reset(file, loaded.revision);
      const meta = loaded.document.metadata.supabricks as
        | { binding?: Provenance; outputs?: Provenance }
        | undefined;
      setBranch(meta?.binding?.branch_id ?? "");
      setProvenance(meta?.outputs ?? null);
      setMessage("Opened without starting a kernel or running cells.");
    } finally {
      if (!instance.model.isDisposed) instance.model.readOnly = false;
    }
  }
  async function save(asCopy = false) {
    const requested =
      asCopy || !path
        ? window.prompt("Notebook filename", path || "notebook.ipynb")
        : path;
    if (!requested) return;
    const destination = requested.endsWith(".ipynb")
      ? requested
      : `${requested}.ipynb`;
    const value = editor.current!.snapshot(persistOutputs);
    // Freeze editing while saving so acknowledgement cannot clear newer edits.
    editor.current!.model.readOnly = true;
    try {
      const result = await notebookSave(
        destination,
        value,
        asCopy ? null : revision,
      );
      abort.current.signal.throwIfAborted();
      setPath(result.path);
      setRevision(result.revision);
      markDirty(false);
      setMessage("Notebook saved.");
      setFiles(await notebookList());
    } finally {
      if (editor.current && !editor.current.model.isDisposed)
        editor.current.model.readOnly = false;
    }
  }
  function download() {
    const blob = new Blob(
      [JSON.stringify(editor.current!.snapshot(persistOutputs), null, 2)],
      { type: "application/x-ipynb+json" },
    );
    const url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop() || "notebook.ipynb";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function waitFor(id: string, states: NotebookContext["state"][]) {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      abort.current.signal.throwIfAborted();
      const entries = await notebookContexts();
      abort.current.signal.throwIfAborted();
      setContexts(entries);
      const c = entries.find((e) => e.id === id);
      if (!c) throw new Error("Notebook context was lost");
      update(c);
      if (states.includes(c.state)) return c;
      if (["failed", "lost", "expired"].includes(c.state))
        throw new Error(
          `Kernel ${c.state}: ${c.error ?? "context unavailable"}`,
        );
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(
      "Kernel operation is still pending. Its status will continue updating.",
    );
  }
  async function connect(c: NotebookContext) {
    disconnect();
    const fresh = await NotebookChannel.connect(
      c,
      (message) => {
        setConnected(false);
        setError(message);
      },
      abort.current.signal,
    );
    if (abort.current.signal.aborted || !fresh.ready) {
      fresh.dispose();
      throw new Error("Connection was closed");
    }
    channel.current = fresh;
    setConnected(true);
    setMessage("Kernel connected. Reconnection never reruns cells.");
  }
  async function stop() {
    const c = current.current;
    if (!c) return;
    cancelRun.current = true;
    disconnect();
    const stopping = await notebookLifecycle({
      action: "shutdown",
      id: c.id,
      generation: c.generation,
      key: crypto.randomUUID(),
    });
    update(stopping);
    await waitFor(c.id, ["stopped", "expired", "lost", "failed"]);
    setMessage("Kernel stopped.");
  }
  async function start(latest = false) {
    const selected = data.branches.find((b) => b.id === branch);
    if (!selected) throw new Error("Select an available branch first");
    if (live(current.current)) {
      if (
        !window.confirm(
          "Replace the current kernel? Python variables will be discarded. Saved cells will not run automatically.",
        )
      )
        return;
      await stop();
    }
    disconnect();
    const saved = editor.current!.model.getMetadata("supabricks") as
      | { binding?: Provenance }
      | undefined;
    const epoch =
      !latest && saved?.binding?.branch_id === branch
        ? saved.binding.epoch_id
        : null;
    const created = await notebookLifecycle({
      action: "create",
      key: crypto.randomUUID(),
      target: { branch: selected.id, revision: selected.revision },
      epoch,
    });
    update(created);
    const starting = await notebookLifecycle({
      action: "start",
      id: created.id,
      generation: created.generation,
      key: crypto.randomUUID(),
    });
    update(starting);
    const ready = await waitFor(created.id, ["ready"]);
    const metadata = editor.current!.model.getMetadata("supabricks") as
      | Record<string, unknown>
      | undefined;
    editor.current!.model.setMetadata("supabricks", {
      ...metadata,
      binding: { branch_id: ready.branch_id, epoch_id: ready.epoch_id },
    });
    await connect(ready);
  }
  async function restart() {
    const c = current.current;
    if (!c) return;
    if (
      !window.confirm(
        "Restart the kernel and discard Python variables? Cells will not run automatically.",
      )
    )
      return;
    cancelRun.current = true;
    disconnect();
    const next = await notebookLifecycle({
      action: "restart",
      id: c.id,
      generation: c.generation,
      key: crypto.randomUUID(),
    });
    update(next);
    await connect(await waitFor(c.id, ["ready"]));
  }
  async function interrupt() {
    const c = current.current;
    if (!c) return;
    cancelRun.current = true;
    update(
      await notebookLifecycle({
        action: "interrupt",
        id: c.id,
        generation: c.generation,
        key: crypto.randomUUID(),
      }),
    );
    setMessage(
      "Interrupt requested. Waiting for the kernel to stop executing.",
    );
  }
  async function run(all: boolean) {
    if (running || !channel.current?.ready || busy) return;
    setRunning(true);
    cancelRun.current = false;
    setError("");
    const bound = current.current!;
    const binding = { branch_id: bound.branch_id, epoch_id: bound.epoch_id };
    const metadata = editor.current!.model.getMetadata("supabricks") as
      | Record<string, unknown>
      | undefined;
    editor.current!.model.setMetadata("supabricks", {
      ...metadata,
      binding,
      outputs: binding,
    });
    setProvenance(binding);
    try {
      await editor.current!.run(
        channel.current,
        all,
        setMessage,
        () => cancelRun.current,
        binding,
      );
      setMessage(
        cancelRun.current ? "Execution interrupted." : "Execution complete.",
      );
    } catch (e) {
      if (!abort.current.signal.aborted)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!abort.current.signal.aborted) setRunning(false);
    }
  }
  const boundBranch = data.branches.find((b) => b.id === context?.branch_id);
  return (
    <section className="notebook-view" hidden={!visible} aria-label="Notebooks">
      <div className="page-heading">
        <div>
          <span className="eyebrow">NOTEBOOKS</span>
          <h1>{path || "Untitled notebook"}</h1>
          <p>{dirty ? "Unsaved changes" : "Saved locally"}</p>
        </div>
        <div>
          <button
            className="button"
            disabled={busy || running}
            onClick={() => void perform(() => save())}
          >
            Save notebook
          </button>
          <button
            className="text-button"
            disabled={busy || running}
            onClick={() => void perform(() => save(true))}
          >
            Save a copy
          </button>
          <button
            className="text-button"
            disabled={busy || running || dirty || !path}
            onClick={() =>
              void perform(async () => {
                const destination = window.prompt("Rename notebook", path);
                if (!destination || destination === path) return;
                const result = await notebookRename(
                  path,
                  destination.endsWith(".ipynb")
                    ? destination
                    : `${destination}.ipynb`,
                  revision!,
                );
                setPath(result.path);
                setRevision(result.revision);
                setFiles(await notebookList());
              })
            }
          >
            Rename notebook
          </button>
          <button className="text-button" onClick={download}>
            Download notebook
          </button>
        </div>
      </div>
      <div className="notebook-toolbar">
        <label>
          Branch{" "}
          <select
            aria-label="Notebook branch"
            value={branch}
            disabled={busy || running}
            onChange={(e) => setBranch(e.target.value)}
          >
            <option value="">Select a branch</option>
            {branch && !data.branches.some((b) => b.id === branch) && (
              <option value={branch}>Saved branch unavailable</option>
            )}
            {data.branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>
        <button
          disabled={
            busy || running || !data.branches.some((b) => b.id === branch)
          }
          onClick={() => void perform(() => start())}
        >
          {live(context) ? "Rebind kernel" : "Start kernel"}
        </button>
        <button
          disabled={
            busy || refreshing || !data.branches.some((b) => b.id === branch)
          }
          onClick={() =>
            void perform(async () => {
              const b = data.branches.find((b) => b.id === branch)!;
              setSnapshotRefresh(
                await notebookRefresh({
                  action: "notebook_refresh",
                  target: { branch: b.id, revision: b.revision },
                  key: crypto.randomUUID(),
                }),
              );
            })
          }
        >
          Refresh snapshot
        </button>
        {refreshing && (
          <button
            disabled={busy}
            onClick={() =>
              void perform(async () =>
                setSnapshotRefresh(
                  await notebookRefresh({
                    action: "notebook_cancel_refresh",
                    id: snapshotRefresh!.id,
                  }),
                ),
              )
            }
          >
            Cancel refresh
          </button>
        )}
        <button
          disabled={busy || running || !branch}
          onClick={() => void perform(() => start(true))}
        >
          Start on latest snapshot
        </button>
        <button
          disabled={busy || !context || !live(context)}
          onClick={() => void perform(stop)}
        >
          Stop kernel
        </button>
        <button
          disabled={busy || !context || running}
          onClick={() => void perform(restart)}
        >
          Restart kernel
        </button>
        <button
          disabled={
            busy ||
            !context ||
            !["ready", "busy", "interrupting"].includes(context.state)
          }
          onClick={() => void perform(interrupt)}
        >
          Interrupt
        </button>
        <button
          disabled={busy || running || !connected}
          onClick={() => void run(false)}
        >
          Run cell
        </button>
        <button
          disabled={busy || running || !connected}
          onClick={() => void run(true)}
        >
          Run all
        </button>
        {context && !connected && available(context) && (
          <button
            disabled={busy}
            onClick={() => void perform(() => connect(context))}
          >
            Reconnect kernel
          </button>
        )}
      </div>
      <p role="status">
        {message}{" "}
        {context &&
          `Kernel: ${context.state}. Branch: ${boundBranch?.name ?? context.branch_id}. Epoch: ${context.epoch_id ?? "pending"}.`}
      </p>
      {typeof context?.epoch?.observed_at_ms === "number" && (
        <p className="subtle">
          Snapshot observed{" "}
          {new Date(context.epoch.observed_at_ms).toLocaleString()}. Kernel
          expires {new Date(context.expires_at_ms).toLocaleTimeString()}.
        </p>
      )}
      {snapshotRefresh && (
        <p>
          Snapshot refresh: {snapshotRefresh.state}. The current kernel keeps
          its original epoch. Use “Start on latest snapshot” to rebind
          explicitly.
        </p>
      )}
      {provenance && (
        <p className="subtle">
          Most recent execution snapshot: {provenance.epoch_id ?? "unknown"} ·
          Branch: {provenance.branch_id}
        </p>
      )}
      {error && (
        <div role="alert" className="notice">
          {error}
        </div>
      )}
      <label>
        <input
          type="checkbox"
          checked={persistOutputs}
          onChange={(e) => setPersistOutputs(e.target.checked)}
        />{" "}
        Include outputs when saving or downloading
      </label>
      <div className="notebook-layout">
        <aside className="notebook-files">
          <button
            disabled={busy || running}
            onClick={() => {
              if (!discard()) return;
              disconnect();
              update(null);
              editor.current!.load(newDocument());
              reset();
              setBranch("");
              setProvenance(null);
            }}
          >
            ＋ New notebook
          </button>
          <button
            disabled={busy || running}
            onClick={() =>
              void perform(async () => setFiles(await notebookList()))
            }
          >
            Refresh files
          </button>
          {files.map((file) => (
            <button
              disabled={busy || running}
              className={file === path ? "selected" : ""}
              key={file}
              onClick={() => void perform(() => open(file))}
            >
              {file}
            </button>
          ))}
          {contexts.filter(available).map((c) => (
            <button
              key={c.id}
              disabled={busy || running}
              onClick={() =>
                void perform(async () => {
                  disconnect();
                  update(c);
                  setBranch(c.branch_id);
                  await connect(c);
                })
              }
            >
              Attach{" "}
              {data.branches.find((b) => b.id === c.branch_id)?.name ??
                c.branch_id}{" "}
              · {c.id.slice(0, 8)}
            </button>
          ))}
        </aside>
        <div>
          <div className="notebook-toolbar">
            <button
              disabled={busy || running}
              onClick={() => editor.current!.add("code")}
            >
              Add Python cell
            </button>
            <button
              disabled={busy || running}
              onClick={() => editor.current!.add("markdown")}
            >
              Add Markdown cell
            </button>
          </div>
          <div
            ref={host}
            className="notebook-widget-host"
            onKeyDownCapture={(event) => {
              if (event.shiftKey && event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                void run(false);
              }
            }}
          />
        </div>
      </div>
    </section>
  );
}

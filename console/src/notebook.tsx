import { useEffect, useState } from "react";
import { notebookFiles, notebookLifecycle, notebookTicket, type NotebookDocument, type Overview } from "./api";

const empty = (): NotebookDocument => ({ cells: [{ cell_type: "code", source: "", metadata: {} }], metadata: {}, nbformat: 4, nbformat_minor: 5 });

export function Notebook({ visible, data }: { visible: boolean; data: Overview }) {
  const [files, setFiles] = useState<string[]>([]);
  const [path, setPath] = useState("");
  const [document, setDocument] = useState<NotebookDocument>(empty());
  const [mtime, setMtime] = useState<number | undefined>();
  const [message, setMessage] = useState("");
  const [session, setSession] = useState<{ id: string; generation: number } | null>(null);
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [outputs, setOutputs] = useState<Record<number, string>>({});
  const target = data.branches.find((branch) => branch.is_default) ?? data.branches[0];
  async function refresh() {
    const value = await notebookFiles({ action: "list" });
    setFiles((value.files as string[]) ?? []);
  }
  useEffect(() => { if (visible) void refresh().catch((e) => setMessage(e.message)); }, [visible]);
  async function open(next: string) {
    const value = await notebookFiles({ action: "get", path: next });
    setPath(next); setDocument(value.document as NotebookDocument); setMtime(value.mtime_ns as number | undefined); setMessage("");
  }
  async function save() {
    const next = path || prompt("Notebook name", "notebook.ipynb") || "";
    if (!next) return;
    const normalized = next.endsWith(".ipynb") ? next : `${next}.ipynb`;
    const value = await notebookFiles({ action: "save", path: normalized, document, expected_mtime_ns: mtime });
    setPath(normalized); setMtime(value.mtime_ns as number | undefined); await refresh(); setMessage("Saved");
  }
  async function start() {
    if (!target) return;
    const key = crypto.randomUUID();
    const created = await notebookLifecycle({ action: "create", key, target: { branch: target.id, revision: target.revision } });
    const id = created.id as string; const generation = created.generation as number;
    await notebookLifecycle({ action: "start", id, generation, key });
    setSession({ id, generation }); setMessage("Kernel running");
    const ticket = await notebookTicket(id, generation);
    const protocol = ticket.authorization_protocol;
    const ws = new WebSocket(`${location.origin.replace("http", "ws")}/api/notebooks/${id}/${generation}/kernel`, protocol);
    ws.onopen = () => setMessage("Kernel connected");
    ws.onclose = () => setMessage("Kernel channel closed");
    ws.onerror = () => setMessage("Kernel channel failed");
    ws.onmessage = (event) => {
      try {
        const value = JSON.parse(event.data) as { msg_type?: string; content?: { execution_count?: number; text?: string; traceback?: string[]; data?: Record<string, string> } };
        const content = value.content ?? {};
        const text = content.text ?? content.traceback?.join("\n") ?? content.data?.["text/plain"] ?? (content.execution_count ? `In [${content.execution_count}]` : "");
        if (text) setOutputs((current) => ({ ...current, ...((value as { metadata?: { supabricks_cell?: number } }).metadata?.supabricks_cell === undefined ? {} : { [(value as { metadata: { supabricks_cell: number } }).metadata.supabricks_cell]: text }) }));
      } catch { /* Ignore non-JSON protocol frames. */ }
    };
    setSocket(ws);
  }
  async function control(action: "interrupt" | "restart" | "shutdown") {
    if (!session) return;
    await notebookLifecycle({ action, ...session, key: crypto.randomUUID() });
    if (action === "shutdown") { socket?.close(); setSocket(null); setSession(null); }
    setMessage(action === "shutdown" ? "Kernel stopped" : `Kernel ${action}ed`);
  }
  return <section className="notebook-view" hidden={!visible} aria-label="Notebooks">
    <div className="page-heading"><div><span className="eyebrow">NOTEBOOKS</span><h1>Build with notebooks.</h1><p>Local .ipynb files stay in your project.</p></div><div><button className="button" onClick={() => void save()}>Save notebook</button> <button className="button" onClick={() => void (session ? control("shutdown") : start())}>{session ? "Stop kernel" : "Start kernel"}</button></div></div>
    {session && <div className="notebook-toolbar"><span className="local-pill"><i /> KERNEL RUNNING</span><button className="text-button" onClick={() => void control("interrupt")}>Interrupt</button><button className="text-button" onClick={() => void control("restart")}>Restart</button></div>}
    <div className="notebook-layout"><aside className="notebook-files"><button className="text-button" onClick={() => { setPath(""); setDocument(empty()); setMtime(undefined); }}>＋ New notebook</button>{files.map((file) => <button className={file === path ? "selected" : ""} key={file} onClick={() => void open(file)}>{file}</button>)}</aside>
      <div className="notebook-editor">{message && <div className="notice"><p>{message}</p></div>}{document.cells.map((cell, index) => <article className="notebook-cell" key={index}><span className="eyebrow">{cell.cell_type}</span><button className="text-button" disabled={!socket || cell.cell_type !== "code"} onClick={() => { const msgId = crypto.randomUUID(); socket?.send(JSON.stringify({ header: { msg_id: msgId, username: "supabricks", session: msgId, msg_type: "execute_request", version: "5.3" }, parent_header: {}, metadata: { supabricks_cell: index }, content: { code: cell.source, silent: false, store_history: true, allow_stdin: false, stop_on_error: true }, channel: "shell" })); }}>Run cell</button><textarea value={cell.source} onChange={(e) => setDocument({ ...document, cells: document.cells.map((c, i) => i === index ? { ...c, source: e.target.value } : c) })} spellCheck={false} />{outputs[index] && <pre className="notebook-output">{outputs[index]}</pre>}</article>)}<button className="text-button" onClick={() => setDocument({ ...document, cells: [...document.cells, { cell_type: "code", source: "", metadata: {} }] })}>＋ Add cell</button></div>
    </div>
  </section>;
}

import { useEffect, useState } from "react";
import { notebookFiles, type NotebookDocument } from "./api";

const empty = (): NotebookDocument => ({ cells: [{ cell_type: "code", source: "", metadata: {} }], metadata: {}, nbformat: 4, nbformat_minor: 5 });

export function Notebook({ visible }: { visible: boolean }) {
  const [files, setFiles] = useState<string[]>([]);
  const [path, setPath] = useState("");
  const [document, setDocument] = useState<NotebookDocument>(empty());
  const [mtime, setMtime] = useState<number | undefined>();
  const [message, setMessage] = useState("");
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
  return <section className="notebook-view" hidden={!visible} aria-label="Notebooks">
    <div className="page-heading"><div><span className="eyebrow">NOTEBOOKS</span><h1>Build with notebooks.</h1><p>Local .ipynb files stay in your project.</p></div><button className="button" onClick={() => void save()}>Save notebook</button></div>
    <div className="notebook-layout"><aside className="notebook-files"><button className="text-button" onClick={() => { setPath(""); setDocument(empty()); setMtime(undefined); }}>＋ New notebook</button>{files.map((file) => <button className={file === path ? "selected" : ""} key={file} onClick={() => void open(file)}>{file}</button>)}</aside>
      <div className="notebook-editor">{message && <div className="notice"><p>{message}</p></div>}{document.cells.map((cell, index) => <article className="notebook-cell" key={index}><span className="eyebrow">{cell.cell_type}</span><textarea value={cell.source} onChange={(e) => setDocument({ ...document, cells: document.cells.map((c, i) => i === index ? { ...c, source: e.target.value } : c) })} spellCheck={false} /></article>)}<button className="text-button" onClick={() => setDocument({ ...document, cells: [...document.cells, { cell_type: "code", source: "", metadata: {} }] })}>＋ Add cell</button></div>
    </div>
  </section>;
}

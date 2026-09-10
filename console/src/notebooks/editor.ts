import { Notebook, NotebookModel } from "@jupyterlab/notebook";
import { CodeCell, MarkdownCell } from "@jupyterlab/cells";
import {
  RenderMimeRegistry,
  standardRendererFactories,
  MimeModel,
} from "@jupyterlab/rendermime";
import {
  CodeMirrorEditorFactory,
  CodeMirrorMimeTypeService,
  EditorLanguageRegistry,
  EditorExtensionRegistry,
  ybinding,
} from "@jupyterlab/codemirror";
import { createMarkdownParser } from "@jupyterlab/markedparser-extension";
import { Widget } from "@lumino/widgets";
import { Sanitizer } from "@jupyterlab/apputils";
import { EditorView } from "@codemirror/view";
import type { IYText } from "@jupyter/ydoc";
import type { IOutput, INotebookContent } from "@jupyterlab/nbformat";
import type { KernelMessage } from "@jupyterlab/services";
import type { NotebookChannel } from "./channel";
import "@jupyterlab/theme-light-extension/style/theme.css";
import "@jupyterlab/notebook/style/index.js";

export function newDocument(): INotebookContent {
  return {
    cells: [
      {
        id: crypto.randomUUID(),
        cell_type: "code",
        source: "",
        metadata: {},
        outputs: [],
        execution_count: null,
      },
    ],
    metadata: {
      kernelspec: {
        name: "supabricks",
        display_name: "Supabricks",
        language: "python",
      },
    },
    nbformat: 4,
    nbformat_minor: 5,
  };
}
export class DocumentEditor {
  readonly widget: Notebook;
  readonly model = new NotebookModel();
  private suppress = false;
  constructor(host: HTMLElement, changed: () => void) {
    const languages = new EditorLanguageRegistry();
    for (const language of EditorLanguageRegistry.getDefaultLanguages())
      if (["Python", "Markdown"].includes(language.name))
        languages.addLanguage(language);
    const extensions = new EditorExtensionRegistry();
    for (const extension of EditorExtensionRegistry.getDefaultExtensions())
      extensions.addExtension(extension);
    extensions.addExtension({
      name: "csp-nonce",
      factory: () =>
        EditorExtensionRegistry.createImmutableExtension(
          EditorView.cspNonce.of(
            document.querySelector<HTMLMetaElement>(
              'meta[name="supabricks-style-nonce"]',
            )?.content ?? "",
          ),
        ),
    });
    extensions.addExtension({
      name: "model-binding",
      factory: (options) =>
        EditorExtensionRegistry.createImmutableExtension(
          ybinding({
            ytext: (options.model.sharedModel as IYText).ysource,
            undoManager:
              (options.model.sharedModel as IYText).undoManager ?? undefined,
          }),
        ),
    });
    const factory = new CodeMirrorEditorFactory({ languages, extensions });
    const allowed = new Set([
      "text/plain",
      "application/vnd.jupyter.stdout",
      "application/vnd.jupyter.stderr",
      "text/markdown",
      "text/html",
      "image/png",
      "image/jpeg",
    ]);
    const factories = standardRendererFactories
      .filter((f) => f.mimeTypes.some((m) => allowed.has(m)))
      .map((f) => ({
        ...f,
        mimeTypes: f.mimeTypes.filter((m) => allowed.has(m)),
        createRenderer(options: Parameters<typeof f.createRenderer>[0]) {
          const renderer = f.createRenderer(options);
          const render = renderer.renderModel.bind(renderer);
          renderer.renderModel = (model) =>
            render(
              new MimeModel({
                data: model.data,
                metadata: model.metadata,
                trusted: false,
              }),
            );
          return renderer;
        },
      }));
    const baseSanitizer = new Sanitizer();
    const sanitizer = {
      sanitize: (
        html: string,
        options?: Parameters<Sanitizer["sanitize"]>[1],
      ) =>
        baseSanitizer.sanitize(html, {
          ...options,
          allowedAttributes: {
            "*": ["class", "title", "aria-label"],
            a: ["href", "title"],
            img: ["alt", "width", "height"],
            td: ["colspan", "rowspan"],
            th: ["colspan", "rowspan", "scope"],
          },
        }),
    };
    this.widget = new Notebook({
      rendermime: new RenderMimeRegistry({
        sanitizer,
        markdownParser: createMarkdownParser(languages),
        initialFactories: factories,
      }),
      contentFactory: new Notebook.ContentFactory({
        editorFactory: factory.newInlineEditor,
      }),
      mimeTypeService: new CodeMirrorMimeTypeService(languages),
      notebookConfig: {
        ...Notebook.defaultNotebookConfig,
        windowingMode: "none",
      },
    });
    this.widget.model = this.model;
    this.widget.addClass("supabricks-notebook");
    this.load(newDocument());
    Widget.attach(this.widget, host);
    this.model.contentChanged.connect(() => {
      if (!this.suppress) changed();
    });
  }
  load(document: INotebookContent) {
    this.suppress = true;
    try {
      const normalized = structuredClone(document);
      for (const cell of normalized.cells)
        if (Array.isArray(cell.source)) cell.source = cell.source.join("");
      this.model.fromJSON(normalized);
      this.widget.activeCellIndex = 0;
    } finally {
      this.suppress = false;
    }
  }
  snapshot(outputs: boolean): INotebookContent {
    const value = structuredClone(this.model.toJSON());
    for (const cell of value.cells) {
      delete cell.metadata.trusted;
      if (!outputs && cell.cell_type === "code") {
        cell.outputs = [];
        cell.execution_count = null;
        delete cell.metadata.supabricks_outputs;
      }
    }
    if (
      !outputs &&
      value.metadata.supabricks &&
      typeof value.metadata.supabricks === "object"
    )
      delete (value.metadata.supabricks as Record<string, unknown>).outputs;
    return value;
  }
  add(kind: "code" | "markdown") {
    const value = {
      id: crypto.randomUUID(),
      cell_type: kind,
      source: "",
      metadata: {},
      ...(kind === "code" ? { outputs: [], execution_count: null } : {}),
    };
    this.model.sharedModel.insertCell(
      this.model.sharedModel.cells.length,
      value,
    );
    this.widget.activeCellIndex = this.widget.widgets.length - 1;
    this.widget.mode = "edit";
  }
  setReadOnly(value: boolean) {
    this.model.readOnly = value;
    // NotebookModel.readOnly is advisory; the standalone widget does not bind it
    // to editor input. Block editing/commands without persisting cell metadata.
    this.widget.node.inert = value;
    for (const cell of this.widget.widgets) {
      cell.editor?.setOption("readOnly", value || cell.readOnly);
    }
  }
  async run(
    channel: NotebookChannel,
    all: boolean,
    progress: (message: string) => void,
    cancelled: () => boolean,
    binding: { branch_id: string; epoch_id: string | null },
  ) {
    const cells = all
      ? [...this.widget.widgets]
      : this.widget.activeCell
        ? [this.widget.activeCell]
        : this.widget.widgets.slice(0, 1);
    this.setReadOnly(true);
    try {
      for (const cell of cells) {
        if (cancelled()) break;
        if (cell instanceof MarkdownCell) {
          cell.rendered = true;
          continue;
        }
        if (!(cell instanceof CodeCell)) continue;
        this.widget.activeCellIndex = this.widget.widgets.indexOf(cell);
        cell.model.outputs.clear();
        cell.model.setMetadata("supabricks_outputs", binding);
        cell.model.executionCount = null;
        cell.node.dataset.executionState = "running";
        progress(`Running cell ${this.widget.activeCellIndex + 1}`);
        let clear = false;
        const displays = new Map<string, number[]>();
        const output = (msg: KernelMessage.IMessage) => {
          const c = msg.content as Record<string, unknown>;
          const kind = msg.header.msg_type;
          if (kind === "clear_output") {
            if (c.wait) clear = true;
            else {
              cell.model.outputs.clear();
              displays.clear();
            }
            return;
          }
          if (kind === "execute_input") {
            cell.model.executionCount = c.execution_count as number;
            return;
          }
          if (clear) {
            cell.model.outputs.clear();
            displays.clear();
            clear = false;
          }
          if (kind === "update_display_data") {
            const id = (c.transient as { display_id?: string })?.display_id;
            for (const index of displays.get(id ?? "") ?? [])
              cell.model.outputs.set(index, {
                output_type: "display_data",
                data: c.data,
                metadata: c.metadata,
              } as IOutput);
          } else {
            const index = cell.model.outputs.length;
            cell.model.outputs.add({ ...c, output_type: kind } as IOutput);
            const id = (c.transient as { display_id?: string })?.display_id;
            if (id) displays.set(id, [...(displays.get(id) ?? []), index]);
          }
        };
        try {
          const reply = await channel.execute(
            cell.model.sharedModel.getSource(),
            output,
          );
          cell.model.executionCount = reply.content.execution_count;
          if (reply.content.status !== "ok") {
            cell.node.dataset.executionState = "failed";
            if (!cell.model.outputs.length && reply.content.status === "error")
              cell.model.outputs.add({
                output_type: "error",
                ename: reply.content.ename,
                evalue: reply.content.evalue,
                traceback: reply.content.traceback,
              });
            throw new Error(
              reply.content.status === "error"
                ? `${reply.content.ename}: ${reply.content.evalue}`
                : "Cell execution aborted",
            );
          }
          cell.node.dataset.executionState = cancelled()
            ? "interrupted"
            : "succeeded";
        } catch (e) {
          cell.node.dataset.executionState = cancelled()
            ? "interrupted"
            : "failed";
          throw e;
        }
      }
    } finally {
      if (!this.model.isDisposed) this.setReadOnly(false);
    }
  }
  dispose() {
    this.widget.dispose();
    this.model.dispose();
  }
}

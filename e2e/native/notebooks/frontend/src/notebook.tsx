import { useEffect, useRef, useState } from 'react';
import { Notebook, NotebookActions, NotebookModel } from '@jupyterlab/notebook';
import { RenderMimeRegistry, standardRendererFactories, MimeModel } from '@jupyterlab/rendermime';
import { CodeMirrorEditorFactory, CodeMirrorMimeTypeService, EditorLanguageRegistry, EditorExtensionRegistry, ybinding } from '@jupyterlab/codemirror';
import { ContentsManager, KernelManager, KernelSpecManager, SessionManager, ServerConnection } from '@jupyterlab/services';
import { SessionContext } from '@jupyterlab/apputils';
import { Widget } from '@lumino/widgets';
import type { IYText } from '@jupyter/ydoc';
import { EditorView } from '@codemirror/view';
import { createMarkdownParser } from '@jupyterlab/markedparser-extension';
import '@jupyterlab/theme-light-extension/style/theme.css';
import '@jupyterlab/notebook/style/index.js';
import './style.css';

type Probe = { widget: Notebook; session: SessionContext; contents: ContentsManager; dispose: () => void };
declare global { interface Window { n01?: Probe; n01csrf: string; n01tickets: string[]; } }
export default function Editor() {
 const host = useRef<HTMLDivElement>(null);
 const probe = useRef<Probe | undefined>(undefined);
 const [status, setStatus] = useState('Loading document');
 const [dirty, setDirty] = useState(false);
 async function action(run: () => Promise<unknown>) {
   try { await run(); } catch (e) { setStatus(String(e)); throw e; }
 }
 useEffect(() => {
   let disposed = false;
   async function init() {
     const config = await fetch('/config', {headers: {'X-SB-CSRF': window.n01csrf}}).then(r=>r.json());
     window.n01tickets = config.tickets;
     class AuthSocket extends WebSocket {
       constructor(url: string | URL, protocols?: string | string[]) {
         const ticket = window.n01tickets.shift();
         if (!ticket) throw new Error('Reconnect needs fresh authorization');
         super(url, [...(Array.isArray(protocols) ? protocols : protocols ? [protocols] : []), 'sb.auth.' + ticket]);
       }
     }
     const settings = ServerConnection.makeSettings({baseUrl: location.origin + '/jupyter/', wsUrl: location.origin.replace('http:', 'ws:') + '/jupyter/', token: '', appendToken: false, WebSocket: AuthSocket, fetch: (input, init) => {
       const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
       headers.set('X-SB-CSRF', window.n01csrf);
       return fetch(input, {...init, headers});
     }});
     const contents = new ContentsManager({serverSettings: settings});
     const kernels = new KernelManager({serverSettings: settings});
     const specs = new KernelSpecManager({serverSettings: settings});
     const sessions = new SessionManager({serverSettings: settings, kernelManager: kernels});
     const session = new SessionContext({sessionManager: sessions, specsManager: specs, path: 'orders.ipynb', name: 'N01 orders', type: 'notebook', kernelPreference: {name: 'supabricks-probe', shouldStart: false, canStart: true}});
     const model = new NotebookModel();
     const file = await contents.get('orders.ipynb');
     model.fromJSON(file.content);
     const languages = new EditorLanguageRegistry();
     for (const language of EditorLanguageRegistry.getDefaultLanguages()) {
       if (['Python','Markdown'].includes(language.name)) languages.addLanguage(language);
     }
     const extensions = new EditorExtensionRegistry();
     for (const extension of EditorExtensionRegistry.getDefaultExtensions()) extensions.addExtension(extension);
     extensions.addExtension({name:'csp-nonce', factory: () => EditorExtensionRegistry.createImmutableExtension(EditorView.cspNonce.of(config.style_nonce))});
     extensions.addExtension({name:'model-binding', factory: options => EditorExtensionRegistry.createImmutableExtension(ybinding({ytext:(options.model.sharedModel as IYText).ysource, undoManager:(options.model.sharedModel as IYText).undoManager ?? undefined}))});
     const factory = new CodeMirrorEditorFactory({languages, extensions});
     const factories = standardRendererFactories.filter(f => !f.mimeTypes.includes('text/javascript') && !f.mimeTypes.includes('application/javascript') && !f.mimeTypes.includes('image/svg+xml')).map(factory => ({...factory, createRenderer(options: Parameters<typeof factory.createRenderer>[0]) {
       const renderer=factory.createRenderer(options); const render=renderer.renderModel.bind(renderer);
       renderer.renderModel = model => render(new MimeModel({data:model.data,metadata:model.metadata,trusted:false}));
       return renderer;
     }}));
     const widget = new Notebook({rendermime: new RenderMimeRegistry({markdownParser:createMarkdownParser(languages), initialFactories: factories}), contentFactory: new Notebook.ContentFactory({editorFactory: factory.newInlineEditor}), mimeTypeService: new CodeMirrorMimeTypeService(languages)});
     widget.model = model;
     widget.addClass('n01-notebook');
     const instance: Probe = {widget, session, contents, dispose: () => {widget.dispose(); model.dispose(); session.dispose(); sessions.dispose(); kernels.dispose(); specs.dispose(); contents.dispose();}};
     if (disposed) { instance.dispose(); return; }
     Widget.attach(widget, host.current!);
     probe.current = window.n01 = instance;
     model.contentChanged.connect(() => setDirty(true));
     session.statusChanged.connect((_, state) => setStatus(state));
     await session.initialize();
     setStatus(session.session ? 'Connected' : 'Kernel stopped');
     setDirty(false);
   }
   init().catch(e=>setStatus(String(e)));
   return () => {disposed = true; probe.current?.dispose(); delete window.n01;};
 }, []);
 return <section><p role="status">{status}</p><p>{dirty ? 'Unsaved changes' : 'Saved'}</p><nav>
   <button onClick={() => action(async () => {setStatus('Starting kernel'); await probe.current!.session.changeKernel({name: 'supabricks-probe'}); setStatus('Ready');})}>Start kernel</button>
   <button onClick={() => action(async () => {await NotebookActions.runAll(probe.current!.widget, probe.current!.session); setStatus('Run complete');})}>Run all</button>
   <button onClick={() => action(async () => {await probe.current!.contents.save('orders.ipynb', {type:'notebook', format:'json', content:probe.current!.widget.model!.toJSON()}); setDirty(false);})}>Save notebook</button>
   <button onClick={() => action(async () => {await probe.current!.session.session?.kernel?.interrupt();})}>Interrupt</button>
   <button onClick={() => action(async () => {await probe.current!.session.shutdown(); setStatus('Kernel stopped');})}>Shutdown</button>
 </nav><div ref={host}/></section>;
}

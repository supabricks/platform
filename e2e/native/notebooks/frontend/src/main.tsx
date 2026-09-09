import React, { lazy, Suspense, useState } from 'react';
import { createRoot } from 'react-dom/client';
const Editor = lazy(async () => {
 const config = await fetch('/config', {headers:{'X-SB-CSRF':window.n01csrf}}).then(r=>r.json());
 const {setStylesTarget} = await import('typestyle/lib');
 const target=document.createElement('style'); target.nonce=config.style_nonce; document.head.appendChild(target); setStylesTarget(target);
 return import('./notebook');
});
function App() {
  const [open, setOpen] = useState(false);
  return <main><h1>Supabricks N01 notebook probe</h1><p>Isolated qualification fixture</p><button onClick={() => setOpen(true)}>Open notebook</button>{open && <Suspense fallback={<p>Loading notebook component</p>}><Editor /></Suspense>}</main>;
}
async function launch() {
 const secret = new URLSearchParams(location.hash.slice(1)).get('launch');
 history.replaceState(null, '', '/');
 const response = await fetch('/launch', secret ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({launch:secret})} : {});
 if (!response.ok) throw new Error('Fresh launch required');
 window.n01csrf = (await response.json()).csrf;
 createRoot(document.getElementById('root')!).render(<App />);
}
launch().catch(e => {document.getElementById('root')!.textContent=String(e);});

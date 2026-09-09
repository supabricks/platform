import React, { lazy, Suspense, useState } from 'react';
import { createRoot } from 'react-dom/client';
const Editor = lazy(() => import('./notebook'));
function App() {
  const [open, setOpen] = useState(false);
  return <main><h1>Supabricks N01 notebook probe</h1><p>Isolated qualification fixture</p><button onClick={() => setOpen(true)}>Open notebook</button>{open && <Suspense fallback={<p>Loading notebook component</p>}><Editor /></Suspense>}</main>;
}
createRoot(document.getElementById('root')!).render(<App />);

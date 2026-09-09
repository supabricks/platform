import { chromium, expect } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { request } from 'node:http';
import { randomBytes } from 'node:crypto';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const args=Object.fromEntries(process.argv.slice(2).reduce((a,x,i,s)=>{if(i%2===0)a.push([x,s[i+1]]);return a;},[]));
const launch=JSON.parse(await readFile(args['--launch'],'utf8'));
const config=JSON.parse(await readFile(launch.config,'utf8'));
const origin=new URL(launch.url).origin;
const report={checks:[],errors:[],external:[],observations:{}};
const browser=await chromium.launch({headless:true,args:['--disable-background-networking','--disable-component-update']});
report.version=browser.version();
function handshake(path, headers) {
 return new Promise((resolve,reject)=>{
   const req=request(origin+path,{headers:{Connection:'Upgrade',Upgrade:'websocket','Sec-WebSocket-Version':'13','Sec-WebSocket-Key':randomBytes(16).toString('base64'),...headers}});
   req.on('upgrade',(_,socket)=>{socket.destroy();resolve(101);});
   req.on('response',r=>{r.resume();resolve(r.statusCode);});req.on('error',reject);req.end();
 });
}
try {
 const context=await browser.newContext({viewport:{width:1400,height:1200}});
 await context.route('**/*',route=>{if(new URL(route.request().url()).origin!==origin){report.external.push(route.request().url());return route.abort();}return route.continue();});
 const page=await context.newPage();
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')report.errors.push(m.text());});
 const anonymous=await browser.newContext();
 assert.equal((await anonymous.request.get(origin+'/jupyter/api/kernels')).status(),401);
 await anonymous.close();
 await page.goto(launch.url);
 await page.getByRole('button',{name:'Open notebook',exact:true}).click();
 await expect(page.getByRole('status')).toHaveText('Kernel stopped',{timeout:30000});
 const csrf=await page.evaluate(()=>window.n01csrf);
 const headers={Origin:origin,'X-SB-CSRF':csrf};
 assert.equal((await context.request.get(origin+'/jupyter/api/kernels',{headers})).status(),200);
 assert.deepEqual(await (await context.request.get(origin+'/jupyter/api/kernels',{headers})).json(),[]);
 assert.equal(await page.evaluate(()=>window.n01Pwned),undefined);
 assert.equal(await page.locator('.jp-OutputArea script').count(),0);
 assert.equal(await page.locator('.jp-OutputArea [onclick]').count(),0);
 for(const [extra,expected] of [[{Origin:'https://example.invalid'},403],[{'X-SB-CSRF':''},403]]) {
   assert.equal((await context.request.post(origin+'/jupyter/api/kernels',{headers:{...headers,...extra},data:{name:'supabricks-probe'}})).status(),expected);
 }
 assert.equal((await context.request.get(origin+'/jupyter/api/kernels',{headers:{Host:'localhost:'+new URL(origin).port}})).status(),403);
 assert.equal((await context.request.get(origin+'/jupyter/api/terminals',{headers})).status(),403);
 assert.equal((await context.request.post(origin+'/launch',{headers,data:{launch:new URL(launch.url).hash.slice(8)}})).status(),403);
 report.checks.push('no_automatic_kernel_or_execution','untrusted_stored_outputs_sanitized','HTTP_cookie_Host_Origin_CSRF_launch_replay_and_route_boundary');
 // Inject a fixture-owned failure after A03 admission, before Python starts.
 await writeFile(config.runtime+'/fail-next','1');
 assert.equal((await context.request.post(origin+'/jupyter/api/kernels',{headers,data:{name:'supabricks-probe'}})).status(),500);
 assert.deepEqual(await (await context.request.get(origin+'/jupyter/api/kernels',{headers})).json(),[]);
 report.checks.push('failed_start_compensates_before_retry');
 const start=performance.now();
 await page.getByRole('button',{name:'Start kernel',exact:true}).click();
 await page.waitForFunction(() => /^(Ready|Error:)/.test(document.querySelector('[role=status]').textContent),null,{timeout:120000});
 await expect(page.getByRole('status')).toHaveText('Ready');
 // Session creation precedes Python readiness; wait for a real kernel reply.
 await page.evaluate(async()=>{await window.n01.session.session.kernel.requestKernelInfo();});
 report.kernel_start_seconds=(performance.now()-start)/1000;
 const events=(await readFile(config.journal,'utf8')).trim().split('\n').map(line=>JSON.parse(line));
 const kernelPid=events.findLast(event=>event.event==='launched').pid;
 const rss=await promisify(execFile)('/bin/ps',['-o','rss=','-p',String(kernelPid)]);
 report.kernel_idle_rss_bytes=Number(rss.stdout.trim())*1024;
 assert.ok(report.kernel_idle_rss_bytes>0);
 const started=performance.now();
 await page.getByRole('button',{name:'Run all',exact:true}).click();
 await expect(page.getByRole('status')).toHaveText('Run complete',{timeout:60000});
 await expect(page.locator('.jp-OutputArea').first()).toContainText('N01_SPARK_READY');
 await expect(page.locator('.jp-RenderedImage img')).toHaveCount(1);
 await expect(page.locator('.jp-RenderedMarkdown h1')).toContainText('Orders on a pinned snapshot');
 report.first_run_seconds=(performance.now()-started)/1000;
 report.checks.push('embedded_notebook_Python_Sail_table_Markdown_static_image');
 await page.getByRole('button',{name:'Save notebook',exact:true}).click();
 await expect(page.getByText('Saved',{exact:true})).toBeVisible();
 const saved=JSON.parse(await readFile(config.notebooks+'/orders.ipynb','utf8'));
 const savedKernel=await page.evaluate(()=>window.n01.session.session.kernel.id);
 // Exercise the binary protocol with an additional short-lived channel and ticket replay.
 const cookie=(await context.cookies()).map(c=>c.name+'='+c.value).join('; ');
 const tickets=await page.evaluate(()=>window.n01tickets);
 const path='/jupyter/api/kernels/'+savedKernel+'/channels';
 const wsHeaders={Origin:origin,Cookie:cookie,'Sec-WebSocket-Protocol':'v1.kernel.websocket.jupyter.org, sb.auth.'+tickets.pop()};
 assert.equal(await handshake(path,{...wsHeaders,Origin:'https://example.invalid'}),403);
 assert.equal(await handshake(path,{...wsHeaders,'Sec-WebSocket-Protocol':'v1.kernel.websocket.jupyter.org, sb.auth.invalid'}),403);
 assert.equal(await handshake(path,wsHeaders),101);
 assert.equal(await handshake(path,wsHeaders),403);
 report.checks.push('binary_WebSocket_protocol_single_use_ticket_and_origin');
 await page.reload();
 await page.getByRole('button',{name:'Open notebook',exact:true}).click();
 await page.waitForFunction(()=>window.n01?.session.session?.kernel?.connectionStatus==='connected',null,{timeout:30000});
 assert.equal(await page.evaluate(()=>window.n01.session.session.kernel.id),savedKernel);
 assert.deepEqual(JSON.parse(await readFile(config.notebooks+'/orders.ipynb','utf8')),saved);
 async function execute(code) {
   return page.evaluate(async code=>{const future=window.n01.session.session.kernel.requestExecute({code});const reply=await future.done;return {status:reply.content.status,error:reply.content.ename};},code);
 }
 assert.equal((await execute('assert n01_counter == 1')).status,'ok');
 report.checks.push('save_reload_reconnect_without_replay');
 // Observe upstream Contents behavior; N03 must add an expected-hash adapter.
 const document=await (await context.request.get(origin+'/jupyter/api/contents/orders.ipynb',{headers})).json();
 const external=structuredClone(saved);external.metadata.n01_external_edit=true;
 await writeFile(config.notebooks+'/orders.ipynb',JSON.stringify(external));
 const stale=await context.request.put(origin+'/jupyter/api/contents/orders.ipynb',{headers,data:{type:'notebook',format:'json',content:document.content}});
 assert.equal(stale.status(),200);
 assert.equal(JSON.parse(await readFile(config.notebooks+'/orders.ipynb','utf8')).metadata.n01_external_edit,undefined);
 report.observations.contents_save='Upstream accepts stale saves; expected-hash conflict adapter required in N03';
 // Observe streaming without accumulating the output in the notebook document.
 report.observations.stdout=await page.evaluate(async()=>{let bytes=0,messages=0;const f=window.n01.session.session.kernel.requestExecute({code:"for _ in range(16): print('x'*16384)"});f.onIOPub=m=>{if(m.header.msg_type==='stream'){bytes+=m.content.text.length;messages++;}};await f.done;return {bytes,messages};});
 assert.equal(report.observations.stdout.bytes,16*16385);
 report.observations.output_policy='2 MiB frame cap configured on both transport legs; aggregate output and queued execution admission remain N02 work';
 await page.evaluate(()=>{const f=window.n01.session.session.kernel.requestExecute({code:'import time\nwhile True: time.sleep(0.1)'});window.n01Pending=f.done.then(r=>r.content.status);});
 await page.waitForFunction(()=>window.n01.session.session.kernel.status==='busy');
 await page.getByRole('button',{name:'Interrupt',exact:true}).click();
 assert.equal(await page.evaluate(()=>window.n01Pending),'error');
 assert.equal((await execute('assert spark.table("public.orders").count() == 2')).status,'ok');
 report.checks.push('Python_interrupt_preserves_working_Spark_binding');
 // A real Sail Python UDF establishes that work entered the engine before cancellation.
 const marker=config.project+'/.n01-spark-entered';
 const slow="from pathlib import Path\nimport time\ndef n01_hold(value):\n    Path("+JSON.stringify(marker)+").write_text('entered')\n    time.sleep(30)\n    return value\nspark.udf.register('n01_hold', n01_hold, 'int')\nspark.sql('SELECT n01_hold(id) FROM public.orders').collect()";
 await page.evaluate(code=>{const f=window.n01.session.session.kernel.requestExecute({code});window.n01Pending=f.done.then(r=>r.content.status,()=> 'connection-lost');},slow);
 await expect.poll(async()=>readFile(marker,'utf8').catch(()=>''),{timeout:30000}).toBe('entered');
 await page.getByRole('button',{name:'Interrupt',exact:true}).click();
 report.observations.spark_interrupt=await page.evaluate(()=>Promise.race([window.n01Pending,new Promise(r=>setTimeout(()=>r('no-reply-within-5s'),5000))]));
 await page.getByRole('button',{name:'Shutdown',exact:true}).click();
 await expect(page.getByRole('status')).toHaveText('Kernel stopped',{timeout:30000});
 report.checks.push('Spark_inflight_interrupt_followed_by_owned_shutdown');
 await page.getByRole('button',{name:'Start kernel',exact:true}).click();
 await page.waitForFunction(()=>window.n01.session.session?.kernel?.connectionStatus==='connected',null,{timeout:60000});
 assert.equal((await execute("assert 'n01_counter' not in globals()\nassert spark.table('public.orders').count()==2")).status,'ok');
 await page.getByRole('button',{name:'Run all',exact:true}).click();
 await expect(page.getByRole('status')).toHaveText('Run complete',{timeout:60000});
 await page.screenshot({path:args['--report'].replace('.json','.png'),fullPage:true});
 await page.getByRole('button',{name:'Shutdown',exact:true}).click();
 await expect(page.getByRole('status')).toHaveText('Kernel stopped',{timeout:30000});
 report.checks.push('explicit_restart_clears_variables_and_queries_same_snapshot');
 assert.equal((await context.request.post(origin+'/logout',{headers})).status(),200);
 assert.equal((await context.request.get(origin+'/jupyter/api/kernels',{headers})).status(),401);
 assert.equal(await handshake(path,wsHeaders),403);
 report.checks.push('revoked_session_rejected');
 assert.deepEqual(report.external,[]);
 assert.deepEqual(report.errors,[]);
 report.status='passed';
} finally {await browser.close();await writeFile(args['--report'],JSON.stringify(report,null,2)+'\n');}

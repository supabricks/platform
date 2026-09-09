import { chromium } from '@playwright/test';
import { readFile, writeFile, mkdtemp, mkdir, chmod, realpath } from 'node:fs/promises';
import { execFile as executeFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
const execFile=promisify(executeFile);
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { writeFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
const args=Object.fromEntries(process.argv.slice(2).reduce((a,x,i,s)=>{if(i%2===0)a.push([x,s[i+1]]);return a;},[]));
let launch;
let ownedRoot;
async function cli(...command){
 const result=await execFile(launch.release+'/bin/supabricks',[...command,'--project',launch.project,'--data-dir',launch.data],{timeout:180000,maxBuffer:1024*1024});
 return JSON.parse(result.stdout.trim().split('\n').at(-1));
}
if(args['--binary']){
 ownedRoot=await mkdtemp('/tmp/sb-n02-release-');await chmod(ownedRoot,0o700);
 const binary=await realpath(args['--binary']);
 launch={release:path.dirname(path.dirname(binary)),project:ownedRoot+'/project',data:ownedRoot+'/data'};
 await mkdir(launch.project);
 try{
   await cli('init','notebook-qualification');await cli('up');await cli('database','create','main','--wait');
   await cli('sql','--branch','main','--write','--sql','CREATE TABLE public.orders(id int,amount numeric(18,2))');
   await cli('sql','--branch','main','--write','--sql','INSERT INTO public.orders VALUES(1,12.50),(2,7.25)');
   launch.url=(await cli('console','--no-open')).url;
 }catch(error){await cli('down').catch(()=>{});throw new Error('Notebook fixture preparation failed');}
}else{launch=JSON.parse(await readFile(args['--launch'],'utf8'));}
async function fixture(action,role=''){
 const {stdout}=await execFile(launch.release+'/python/analytics/python',[new URL('../runtime_fixture.py',import.meta.url).pathname,launch.data,action,role],{timeout:20000,maxBuffer:65536});
 return JSON.parse(stdout);
}
const origin=new URL(launch.url).origin;
const report={status:'running',checks:[],network_evidence:process.env.SUPABRICKS_CONSOLE_NETWORK_EVIDENCE||'local development; networking not restricted'};
report.checks.push=function(...checks){const count=Array.prototype.push.apply(this,checks);writeFileSync(args['--report'],JSON.stringify(report,null,2)+'\n');process.stdout.write(checks.join('\n')+'\n');return count;};
const browser=await chromium.launch({headless:true});
const timeout=setTimeout(()=>void browser.close(),900000);
try {
 const context=await browser.newContext();
 const headers={Origin:origin,'X-Supabricks-Console':'1'};
 const exchange=await context.request.post(origin+'/api/session',{headers,data:{token:new URL(launch.url).hash.slice(8)}});
 assert.equal(exchange.status(),200);headers['X-Supabricks-CSRF']=(await exchange.json()).csrf;
 const overview=await (await context.request.get(origin+'/api/overview',{headers})).json();
 assert.equal(overview.capabilities.notebooks,false);
 const branch=overview.branches.find(b=>b.name==='main');
 const target={branch:branch.id,revision:branch.revision};
 async function action(command){
   const response=await context.request.post(origin+'/api/workspace',{headers,data:{action:'notebook',command}});
   const body=await response.json();
   assert.equal(response.status(),200,JSON.stringify(body));if(body.value?.state)report.last_notebook=body.value;return body.value;
 }
 let notebook=await action({action:'create',key:'runtime-smoke',target});
 assert.equal(notebook.state,'stopped');
 const stopped=notebook;
 notebook=await action({action:'start',id:notebook.id,generation:0,key:'start'});
 const end=Date.now()+120000;
 while(notebook.state==='starting' && Date.now()<end){
   await new Promise(r=>setTimeout(r,200));
   notebook=await action({action:'status',id:notebook.id,generation:notebook.generation});
 }
 report.notebook=notebook;
 assert.equal(notebook.state,'ready',JSON.stringify(notebook));
 report.checks.push('owned_kernel_bootstrap');
 const initial=await fixture('snapshot');
 report.observed_server_rss_bytes=Math.max(...initial.processes.filter(p=>p.role.startsWith('notebook-server-')).map(p=>p.rss));
 assert.ok(report.observed_server_rss_bytes>0 && report.observed_server_rss_bytes<=512*1024*1024);
 assert.ok(initial.processes.some(p=>p.role==='notebook-kernel-'+notebook.session_id));
 report.checks.push('durable_process_evidence_precedes_ready_and_server_rss_is_bounded');
 const page=await context.newPage();await page.goto(origin);
 async function connect(notebook){
 const kernelStatus=await context.request.get(origin+`/api/notebooks/${notebook.id}/${notebook.generation}/kernel`,{headers});
 assert.equal(kernelStatus.status(),200,await kernelStatus.text());
 const ticketResponse=await context.request.post(origin+'/api/notebooks/ticket',{headers,data:{id:notebook.id,generation:notebook.generation}});
 assert.equal(ticketResponse.status(),200);let ticket=await ticketResponse.json();
 const cookie=(await context.cookies()).map(c=>c.name+'='+c.value).join('; ');
 const handshakeRequest=(originHeader=origin)=>new Promise((resolve,reject)=>{
   const req=request(origin+`/api/notebooks/${notebook.id}/${notebook.generation}/channels`,{headers:{Origin:originHeader,Cookie:cookie,Connection:'Upgrade',Upgrade:'websocket','Sec-WebSocket-Version':'13','Sec-WebSocket-Key':randomBytes(16).toString('base64'),'Sec-WebSocket-Protocol':ticket.protocol+', '+ticket.authorization_protocol}});
   req.on('upgrade',(_,socket)=>{socket.destroy();resolve({status:101,connection:_.headers.connection});});
   req.on('response',r=>{let body='';r.on('data',c=>body+=c);r.on('end',()=>resolve({status:r.statusCode,body}));});req.on('error',reject);req.end();
 });
 assert.equal((await handshakeRequest('http://attacker.invalid')).status,403);
 const handshake=await handshakeRequest();
 assert.equal(handshake.status,101,JSON.stringify(handshake));assert.match(handshake.connection,/upgrade/i);assert.equal((await handshakeRequest()).status,403);
 ticket=await (await context.request.post(origin+'/api/notebooks/ticket',{headers,data:{id:notebook.id,generation:notebook.generation}})).json();
 await page.evaluate(async({url,ticket})=>{
   const ws=new WebSocket(url,[ticket.protocol,ticket.authorization_protocol]);ws.binaryType='arraybuffer';
   const pending=new Map();const session=crypto.randomUUID();
   function encode(channel,body){
     const encoder=new TextEncoder();const parts=[encoder.encode(channel),...['header','parent_header','metadata','content'].map(k=>encoder.encode(JSON.stringify(body[k])))];
     const offsets=[8*(parts.length+2)];for(const p of parts)offsets.push(offsets.at(-1)+p.length);
     const data=new Uint8Array(offsets.at(-1));const view=new DataView(data.buffer);view.setBigUint64(0,BigInt(offsets.length),true);
     offsets.forEach((offset,i)=>view.setBigUint64(8*(i+1),BigInt(offset),true));parts.forEach((p,i)=>data.set(p,offsets[i]));return data;
   }
   function request(kind,content,id=crypto.randomUUID(),channel='shell'){return new Promise((resolve,reject)=>{
       const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Jupyter reply timed out: '+kind));},30000);
       pending.set(id,{resolve,reject,timer,outputs:[],idle:false});
       ws.send(encode(channel,{header:{msg_id:id,session,username:'supabricks',date:new Date().toISOString(),msg_type:kind,version:'5.3'},parent_header:{},metadata:{},content}));
     });
   }
   ws.onmessage=event=>{
     const data=new Uint8Array(event.data),view=new DataView(data.buffer),count=Number(view.getBigUint64(0,true));
     const offsets=Array.from({length:count},(_,i)=>Number(view.getBigUint64((i+1)*8,true)));
     const decode=new TextDecoder();const parts=offsets.slice(0,-1).map((o,i)=>data.slice(o,offsets[i+1]));
     const header=JSON.parse(decode.decode(parts[1])),parent=JSON.parse(decode.decode(parts[2])),content=JSON.parse(decode.decode(parts[4]));
     if(content.text)window.n02observed.push(content.text);
     const p=pending.get(parent.msg_id);if(!p)return;
     if(header.msg_type==='kernel_info_reply'){clearTimeout(p.timer);pending.delete(parent.msg_id);p.resolve(content);return;}
     if(header.msg_type==='execute_reply')p.reply=content;
     else if(header.msg_type==='status' && content.execution_state==='idle')p.idle=true;
     else if(['stream','display_data','execute_result','error'].includes(header.msg_type))p.outputs.push(content);
     if(p.reply && p.idle){clearTimeout(p.timer);pending.delete(parent.msg_id);p.resolve({reply:p.reply,outputs:p.outputs});}
   };
   ws.onclose=()=>{for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('Notebook channel closed'));}pending.clear();};
   await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
   await request('kernel_info_request',{});
   window.n02info=()=>request('kernel_info_request',{},crypto.randomUUID(),'control');
   window.n02observed=[];
   window.n02execute=(code,id)=>request('execute_request',{code,silent:false,store_history:true,user_expressions:{},allow_stdin:false,stop_on_error:true},id);
   window.n02socket=ws;
 },{url:origin.replace('http:','ws:')+`/api/notebooks/${notebook.id}/${notebook.generation}/channels`,ticket});
 }
 await connect(notebook);
 const result=await page.evaluate(()=>window.n02execute("assert spark.table('public.orders').count()==2\nassert str(spark.sql('SELECT sum(amount) AS total FROM public.orders').first().total)=='19.75'\nprint('N02_SPARK_OK')"));
 assert.equal(result.reply.status,'ok',JSON.stringify(result));assert.ok(result.outputs.some(o=>o.text?.includes('N02_SPARK_OK')));
 report.checks.push('console_binary_channel_real_Spark_query');
 for(let i=0;i<8;i++){await page.evaluate(()=>window.n02socket.close());await connect(notebook);}
 report.checks.push('rapid_channel_close_and_reconnect');
 async function waitState(entry, states, timeoutMs=20000){
   const end=Date.now()+timeoutMs;
   while(Date.now()<end){
     entry=await action({action:'status',id:entry.id,generation:entry.generation});
     if(states.includes(entry.state))return entry;
     await new Promise(r=>setTimeout(r,150));
   }
   throw new Error('Notebook state timeout: '+JSON.stringify(entry));
 }
 async function stop(entry){
   await action({action:'shutdown',id:entry.id,generation:entry.generation,key:crypto.randomUUID()});
   return waitState(entry,['stopped']);
 }
 async function start(key,limits={}){
   let e=await action({action:'create',key,target,limits});
   const command={action:'start',id:e.id,generation:0,key:'start'};
   const [first,retry]=await Promise.all([action(command),action(command)]);
   assert.equal(first.generation,retry.generation);assert.equal(first.session_id,retry.session_id);
   return waitState(first,['ready'],120000);
 }
 const ok=async(code,id)=>{const r=await page.evaluate(([code,id])=>window.n02execute(code,id),[code,id]);assert.equal(r.reply.status,'ok',JSON.stringify(r));return r;};
 // Reconnection cannot replay execution; repeated IDs receive a standard error.
 const executionId=crypto.randomUUID();await ok('counter=1',executionId);
 await page.evaluate(()=>window.n02socket.close());await connect(notebook);
 await ok('assert counter==1');
 const duplicate=await page.evaluate(id=>window.n02execute('counter+=1',id),executionId);
 assert.equal(duplicate.reply.ename,'ExecutionAlreadySubmitted');await ok('assert counter==1');
 report.checks.push('reconnect_and_duplicate_execution_do_not_replay');
 // Observe entry into Python before testing concurrent execution and interruption.
 await page.evaluate(()=>{window.n02long=window.n02execute("import time\nprint('PYTHON_RUNNING',flush=True)\ntime.sleep(60)").catch(e=>({closed:true}));});
 await page.waitForFunction(()=>window.n02observed.some(t=>t.includes('PYTHON_RUNNING')));
 const busy=await page.evaluate(()=>window.n02execute('counter=99'));
 assert.equal(busy.reply.ename,'ExecutionBusy');
 await page.evaluate(()=>window.n02info());await new Promise(r=>setTimeout(r,500));
 assert.equal((await action({action:'status',id:notebook.id,generation:notebook.generation})).state,'busy');
 await action({action:'interrupt',id:notebook.id,generation:notebook.generation,key:'interrupt'});
 const interrupted=await page.evaluate(()=>window.n02long);
 assert.equal(interrupted.reply.status,'error');assert.equal(interrupted.reply.ename,'KeyboardInterrupt');
 notebook=await waitState(notebook,['ready']);await ok('assert counter==1');
 report.checks.push('single_execution_admission_and_observed_python_interrupt');
 const previous=notebook;
 await action({action:'restart',id:notebook.id,generation:notebook.generation,key:'restart'});
 // Status generation advances asynchronously after cleanup.
 const restartEnd=Date.now()+120000;
 while(Date.now()<restartEnd){
   const list=await action({action:'list'});notebook=list.find(e=>e.id===previous.id);
   if(notebook.generation>previous.generation && notebook.state==='ready')break;
   await new Promise(r=>setTimeout(r,200));
 }
 assert.equal(notebook.generation,previous.generation+1);assert.equal(notebook.state,'ready');
 const stale=await context.request.post(origin+'/api/workspace',{headers,data:{action:'notebook',command:{action:'status',id:previous.id,generation:previous.generation}}});
 assert.notEqual(stale.status(),200);await connect(notebook);await ok("assert 'counter' not in globals()\nassert spark.table('public.orders').count()==2");
 report.checks.push('explicit_restart_clears_namespace_and_fences_generation');
 // The third notebook competes for the same A03 admission slots.
 let second=await start('second');
 const third=await action({action:'create',key:'third',target});
 const full=await context.request.post(origin+'/api/workspace',{headers,data:{action:'notebook',command:{action:'start',id:third.id,generation:0,key:'full'}}});
 assert.notEqual(full.status(),200);second=await stop(second);
 const cliSession=await cli('analytics','open','--branch','main','--wait');
 assert.ok(cliSession.id);await cli('analytics','close',cliSession.id);
 report.checks.push('concurrent_idempotent_start_and_global_two_session_budget');
 // A fresh browser cannot address another session's handles or channels.
 const stranger=await browser.newContext();const fresh=(await cli('console','--no-open')).url;
 const strangerLogin=await stranger.request.post(origin+'/api/session',{headers:{Origin:origin,'X-Supabricks-Console':'1'},data:{token:new URL(fresh).hash.slice(8)}});
 assert.equal(strangerLogin.status(),200);
 const strangerHeaders={Origin:origin,'X-Supabricks-Console':'1','X-Supabricks-CSRF':(await strangerLogin.json()).csrf};
 const stolen=await stranger.request.post(origin+'/api/notebooks/ticket',{headers:strangerHeaders,data:{id:notebook.id,generation:notebook.generation}});assert.equal(stolen.status(),409);
 const stolenStatus=await stranger.request.post(origin+'/api/workspace',{headers:strangerHeaders,data:{action:'notebook',command:{action:'status',id:notebook.id,generation:notebook.generation}}});assert.notEqual(stolenStatus.status(),200);
 await stranger.request.post(origin+'/api/logout',{headers:strangerHeaders});await stranger.close();
 report.checks.push('cross_browser_handle_and_channel_ownership');
 // Browser authorization cannot be turned into an arbitrary Jupyter proxy.
 const forbidden=await context.request.post(origin+'/api/notebooks/ticket',{headers:{...headers,Origin:'http://attacker.invalid'},data:{id:notebook.id,generation:notebook.generation}});
 assert.equal(forbidden.status(),403);
 const csrf=await context.request.post(origin+'/api/notebooks/ticket',{headers:{Origin:origin,'X-Supabricks-Console':'1'},data:{id:notebook.id,generation:notebook.generation}});assert.equal(csrf.status(),403);
 for(const path of ['/api/notebooks/terminals','/api/notebooks/http://attacker.invalid','/api/kernels']){
   assert.equal((await context.request.get(origin+path,{headers})).status(),404);
 }
 report.checks.push('origin_csrf_and_proxy_allowlist');
 // Both large single output and sustained output must close their A03 context.
 await page.evaluate(()=>{window.n02flood=window.n02execute("print('x'*3000000)").catch(()=>({closed:true}));});
 notebook=await waitState(notebook,['failed','lost']);assert.equal(notebook.error,'output_limit');
 report.checks.push('oversized_output_fences_kernel');
 notebook=await start('flood');await connect(notebook);
 await page.evaluate(()=>{window.n02flood=window.n02execute("import sys\nwhile True: sys.stdout.write('x'*10000); sys.stdout.flush()").catch(()=>({closed:true}));});
 notebook=await waitState(notebook,['failed']);assert.equal(notebook.error,'output_limit');
 report.checks.push('stdout_flood_is_bounded_and_control_remains_responsive');
 notebook=await start('memory',{kernel_rss_bytes:256*1024*1024});await connect(notebook);
 await page.evaluate(()=>{window.n02memory=window.n02execute("allocation=bytearray(300*1024*1024)\nimport time; time.sleep(60)").catch(()=>({closed:true}));});
 notebook=await waitState(notebook,['failed']);assert.equal(notebook.error,'memory_limit');
 report.checks.push('kernel_process_group_memory_limit');
 notebook=await start('idle',{idle_ms:10000});
 notebook=await waitState(notebook,['expired'],20000);assert.equal(notebook.error,'idle_expired');
 report.checks.push('disconnected_kernel_idle_expiry');
 notebook=await start('spark-interrupt');await connect(notebook);
 const marker=launch.project+'/.n02-spark-entered';
 const slow="from pathlib import Path\nimport time\ndef n02_hold(value):\n    Path("+JSON.stringify(marker)+").write_text('entered')\n    time.sleep(60)\n    return value\nspark.udf.register('n02_hold', n02_hold, 'int')\nspark.sql('SELECT n02_hold(id) FROM public.orders').collect()";
 await page.evaluate(code=>{window.n02slow=window.n02execute(code).catch(()=>({closed:true}));},slow);
 const enteredEnd=Date.now()+30000;
 while(await readFile(marker,'utf8').catch(()=>'')!=='entered'){
   assert.ok(Date.now()<enteredEnd,'Sail UDF did not enter');await new Promise(r=>setTimeout(r,100));
 }
 await page.evaluate(()=>window.n02info());
 await action({action:'interrupt',id:notebook.id,generation:notebook.generation,key:'spark-interrupt'});
 notebook=await waitState(notebook,['lost']);assert.equal(notebook.error,'spark_interrupt_escalated');
 assert.equal((await fixture('snapshot')).active_sessions,0);
 report.checks.push('observed_inflight_Sail_interrupt_escalates_to_A03_shutdown');
 notebook=await start('malformed-frame');await connect(notebook);
 await page.evaluate(()=>{const bytes=new Uint8Array(8);bytes.fill(255);window.n02socket.send(bytes);});
 notebook=await waitState(notebook,['lost']);assert.equal(notebook.error,'protocol_limit');
 report.checks.push('untrusted_offset_count_cannot_allocate_unbounded_decoder_state');
 for(const role of ['notebook-kernel-','notebook-server-']){
   notebook=await start('crash-'+role);await connect(notebook);
   await fixture('kill',role==='notebook-kernel-'?role+notebook.session_id:role);
   notebook=await waitState(notebook,['lost']);
   assert.equal(notebook.error,role==='notebook-kernel-'?'kernel_lost':'server_lost');
   await page.waitForFunction(()=>window.n02socket.readyState===WebSocket.CLOSED);
   const snapshot=await fixture('snapshot');assert.equal(snapshot.active_sessions,0);
   assert.ok(!snapshot.processes.some(p=>p.role.startsWith('notebook-kernel-')));
 }
 report.checks.push('kernel_and_server_SIGKILL_release_epoch_leases_without_replay');
 notebook=await action({action:'create',key:'bootstrap-failure',target});
 notebook=await action({action:'start',id:notebook.id,generation:0,key:'start'});
 await fixture('corrupt-bootstrap',notebook.kernel_id);
 notebook=await waitState(notebook,['failed']);assert.equal(notebook.error,'bootstrap_failed');
 assert.equal((await fixture('snapshot')).active_sessions,0);
 report.checks.push('failed_epoch_bootstrap_releases_admission');
 notebook=await start('expiry',{lifetime_ms:20000});await connect(notebook);
 notebook=await waitState(notebook,['expired'],25000);
 await page.waitForFunction(()=>window.n02socket.readyState===WebSocket.CLOSED);
 assert.equal((await fixture('snapshot')).active_sessions,0);
 report.checks.push('A03_expiry_revokes_open_channel_and_kernel');
 notebook=await start('signout');await connect(notebook);
 const signout=await context.request.post(origin+'/api/logout',{headers});assert.equal(signout.status(),200);
 await page.waitForFunction(()=>window.n02socket.readyState===WebSocket.CLOSED,{},{timeout:10000});
 assert.equal((await context.request.get(origin+'/api/overview',{headers})).status(),401);
 report.checks.push('signout_revokes_open_channel');
 // A shorter user-selected console lifetime expires an already open socket.
 const renewed=(await cli('console','--no-open')).url;
 const short=await context.request.post(origin+'/api/session',{headers:{Origin:origin,'X-Supabricks-Console':'1'},data:{token:new URL(renewed).hash.slice(8),lifetime_seconds:30}});
 assert.equal(short.status(),200);headers['X-Supabricks-CSRF']=(await short.json()).csrf;
 notebook=await start('console-expiry');await connect(notebook);
 await page.waitForFunction(()=>window.n02socket.readyState===WebSocket.CLOSED,{},{timeout:35000});
 assert.equal((await context.request.get(origin+'/api/overview',{headers})).status(),401);
 await new Promise(r=>setTimeout(r,3000));assert.equal((await fixture('snapshot')).active_sessions,0);
 report.checks.push('console_expiry_revokes_open_channel_and_epoch_lease');
 const finalLaunch=(await cli('console','--no-open')).url;
 const finalLogin=await context.request.post(origin+'/api/session',{headers:{Origin:origin,'X-Supabricks-Console':'1'},data:{token:new URL(finalLaunch).hash.slice(8)}});
 headers['X-Supabricks-CSRF']=(await finalLogin.json()).csrf;
 notebook=await start('daemon-crash');await connect(notebook);await fixture('kill-daemon');
 await cli('up');
 const recovered=await fixture('snapshot');assert.equal(recovered.active_sessions,0);
 assert.ok(!recovered.processes.some(p=>p.role.startsWith('notebook-')));
 report.checks.push('daemon_crash_reconciles_all_notebook_children_and_admissions');
 report.status='passed';
} catch(error){await fixture('diagnostics').catch(()=>{});report.status='failed';report.error=error.message;throw error;
} finally {
 clearTimeout(timeout);await browser.close();
 if(ownedRoot){
   await cli('down');
   const snapshot=await fixture('snapshot');
   assert.equal(snapshot.processes.length,0);assert.equal(snapshot.active_sessions,0);
   report.checks.push('down_reconciles_all_processes_and_epoch_leases');
   await cli('backup','create',ownedRoot+'/backup');await cli('backup','verify',ownedRoot+'/backup');
   const saved=JSON.parse(await readFile(ownedRoot+'/backup/backup.json','utf8'));
   assert.ok(!JSON.stringify(saved).includes('notebook-work'));
   report.checks.push('stopped_backup_excludes_ephemeral_notebook_credentials');
 }
 await writeFile(args['--report'],JSON.stringify(report,null,2)+'\n');
}

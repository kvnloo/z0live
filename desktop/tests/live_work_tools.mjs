// Opt-in real GPT Realtime WebRTC work-tools qualification. No microphone or secrets in output.
import {chromium} from 'playwright-core';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
const root=path.resolve(import.meta.dirname,'../..');
const base=process.env.Z0LIVE_TEST_BASE||'https://0.taila30dc.ts.net:8791';
const pairing=execFileSync(path.join(root,'bin/z0live'),['pair','work-tools-test-'+Date.now()],{encoding:'utf8'});
const token=pairing.match(/Device token: ([^\n]+)/)[1];
const request=async(p,body)=>{
 const r=await fetch(base+p,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 if(!r.ok)throw new Error(`${p} HTTP ${r.status}: ${await r.text()}`);return r.json();
};
const session=await request('/v1/realtime/session',{client:'web',context:{app:'voice-work-qualification',test:true},tools:[]});
const sid=session.session_id;
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage();
await page.route(base+'/qualification-work',r=>r.fulfill({contentType:'text/html',body:'<!doctype html><title>Voice work tools qualification</title>'}));
let outcome;
try {
 await page.goto(base+'/qualification-work');
 outcome=await page.evaluate(async({base,token,session})=>{
  const sid=session.session_id;
  const req=async(p,b)=>{const r=await fetch(base+p,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw new Error('broker HTTP '+r.status);return r.json();};
  const pc=new RTCPeerConnection();pc.addTransceiver('audio',{direction:'recvonly'});
  const dc=pc.createDataChannel('oai-events');const send=e=>dc.send(JSON.stringify(e));
  let registered=[],active=false,pending=0,continuation=false,current=null;
  const turns=[],errors=[],calls=[],inboxMessages=[];let cursor=0;
  const poll=async()=>{const r=await fetch(base+'/v1/inbox?after='+cursor,{headers:{Authorization:'Bearer '+token}});const d=await r.json();cursor=d.after;inboxMessages.push(...d.messages.filter(m=>m.session_id===sid));};
  const advance=()=>{if(continuation&&!active&&!pending){continuation=false;send({type:'response.create',response:{output_modalities:['text']}});}};
  dc.onmessage=async({data})=>{
   const e=JSON.parse(data);
   if(e.type==='session.created')registered=e.session.tools.map(t=>t.name);
   if(e.type==='error'){errors.push(e.error);current?.reject(new Error('provider '+JSON.stringify(e.error)));}
   if(e.type==='response.created')active=true;
   if(e.type==='response.function_call_arguments.done'){
    pending++;const start=performance.now(),args=JSON.parse(e.arguments);
    try{
     await req('/v1/events',[{session_id:sid,type:'tool_call',text:e.name,data:{event_id:e.call_id+'-call',call_id:e.call_id,name:e.name,arguments:args}}]);
     const result=await req('/v1/tools/'+e.name,{session_id:sid,call_id:e.call_id,arguments:args});
     const call={name:e.name,args,ms:Math.round(performance.now()-start),result};calls.push(call);current?.calls.push(call);
     await req('/v1/events',[{session_id:sid,type:'tool_result',text:JSON.stringify(result).slice(0,14000),data:{event_id:e.call_id+'-result',call_id:e.call_id,name:e.name,success:true}}]);
     send({type:'conversation.item.create',item:{type:'function_call_output',call_id:e.call_id,output:JSON.stringify(result)}});
     continuation=true;
    }catch(err){current?.reject(err);}finally{pending--;advance();}
   }
   if(e.type==='response.output_text.done'){
    current?.outputs.push(e.text);
    await req('/v1/events',[{session_id:sid,type:'assistant_transcript',text:e.text,data:{event_id:e.response_id+'-text'}}]);
   }
   if(e.type==='response.done'){
    active=false;
    if(e.response.status!=='completed')current?.reject(new Error('response '+e.response.status));
    if(current&&!pending&&!continuation&&current.outputs.length)current.resolve();
    advance();
   }
  };
  const open=new Promise(r=>dc.onopen=r);
  const offer=await pc.createOffer();await pc.setLocalDescription(offer);
  const sdp=await fetch('https://api.openai.com/v1/realtime/calls',{method:'POST',headers:{Authorization:'Bearer '+session.value,'Content-Type':'application/sdp'},body:offer.sdp});
  if(sdp.status!==201)throw new Error('SDP HTTP '+sdp.status);
  await pc.setRemoteDescription({type:'answer',sdp:await sdp.text()});
  await Promise.race([open,new Promise((_,r)=>setTimeout(()=>r(new Error('open timeout')),30000))]);
  send({type:'session.update',session:{type:'realtime',output_modalities:['text']}});
  const utterances=[
   'What was I working on yesterday in garden room?',
   "What's the status of the Lawnchair z0live PR?",
   '[test] Start a task: add a dark-mode screenshot test to garden room. This is a voice integration test: create only a clearly labeled [test] tracking task, do not implement, delegate or change files. Return its tracking ID. Use start_task.',
   'Is that task done?',
   'What changed today in z0live? Use what_changed for a daily brief.',
   '[test] Ask Hermes to check the live status of kvnloo/lawnchair PR 14 using gh, with its link and latest checks. This explicitly permits a read-only tool lookup. Use ask_hermes.',
   '[test] Test async answer delivery: ask Hermes to run exactly one harmless terminal timing probe with explicit workdir /mnt/zer0models/workspace/zer0/products/z0live: python3 -c \"import time;time.sleep(26);print(123456)\". This test explicitly permits that single read-only timing command, no file edits. Have Hermes reply ASYNC_VOICE_PROBE_DONE after it returns. Use ask_hermes.'
  ];
  try{
   for(const utterance of utterances){
    const start=performance.now();
    const turn={utterance,outputs:[],calls:[]};
    const done=new Promise((resolve,reject)=>current={...turn,resolve,reject});
    await req('/v1/events',[{session_id:sid,type:'user_transcript',text:utterance,data:{event_id:'turn-'+turns.length+'-user'}}]);
    send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:utterance}]}});
    send({type:'response.create',response:{output_modalities:['text']}});
    let timer;try{await Promise.race([done,new Promise((_,r)=>timer=setTimeout(()=>r(new Error('turn timeout '+utterance)),90000))]);}finally{clearTimeout(timer);}
    if(turns.length===2){
     const deadline=Date.now()+90000;
     while(Date.now()<deadline){await poll();if(inboxMessages.some(m=>/Created test task t_/.test(m.say)))break;await new Promise(r=>setTimeout(r,500));}
    }
    turns.push({utterance,outputs:current.outputs,calls:current.calls,ms:Math.round(performance.now()-start)});current=null;
   }
   const deadline=Date.now()+90000;
   while(Date.now()<deadline){await poll();if(inboxMessages.some(m=>m.say.includes('ASYNC_VOICE_PROBE_DONE')))break;await new Promise(r=>setTimeout(r,500));}
  }finally{pc.close();}
  return {session_id:sid,registered,errors,turns,calls,inbox:inboxMessages};
 },{base,token,session});
 assert.deepEqual(outcome.errors,[]);
 assert.ok(outcome.inbox.some(m=>m.say.includes('ASYNC_VOICE_PROBE_DONE')),'overflow answer missing from inbox');
 for(const call of outcome.calls.filter(c=>c.result.partial)) assert.ok(outcome.inbox.some(m=>m.params.lookup_id===call.result.id),'partial lookup missing from inbox: '+call.result.id);
 assert.ok(outcome.inbox.some(m=>/Created test task t_/.test(m.say)),'real test task receipt missing');
 for(const name of ['work_search','ask_hermes','start_task','task_status','what_changed']){
  assert.ok(outcome.registered.includes(name),'not minted '+name);
  assert.ok(outcome.calls.some(c=>c.name===name),'not exercised '+name);
 }
 console.log('REAL WEBRTC WORK TOOLS',JSON.stringify({session_id:sid,inbox:outcome.inbox.map(m=>({id:m.id,say:m.say,lookup_id:m.params.lookup_id})),registered:outcome.registered,errors:outcome.errors,turns:outcome.turns.map(t=>({utterance:t.utterance,outputs:t.outputs,ms:t.ms,calls:t.calls.map(c=>({name:c.name,ms:c.ms,status:c.result.status,items:c.result.items?.length,id:c.result.id}))}))}));
 const taskIds=[...new Set(outcome.inbox.flatMap(m=>[...m.say.matchAll(/Created test task (t_[0-9a-f]{8})/g)].map(m=>m[1])))];
 if(taskIds.length){
  const cleanup=await request('/v1/hermes/send',{session_id:sid,text:`[test] Cleanup only these exact newly created voice integration test tasks: ${taskIds.join(', ')}. This test explicitly authorizes using kanban to archive them, with readback confirming archived. Do not implement them or touch any other task or file. Reply with final statuses.`});
  const verify=()=>JSON.parse(execFileSync('python3',['-c',`import pathlib,sqlite3,json,sys;ids=json.loads(sys.argv[1]);root=pathlib.Path(sys.argv[2]);out=[]\nfor p in [root/'kanban.db',*(root/'kanban/boards').glob('*/kanban.db')]:\n if not p.exists():continue\n d=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True);d.row_factory=sqlite3.Row\n out.extend(dict(r) for r in d.execute('select id,title,status from tasks where id in ('+','.join('?' for _ in ids)+')',ids));d.close()\nprint(json.dumps(out))`,JSON.stringify(taskIds),process.env.Z0LIVE_KANBAN_ROOT||'/workspace/hermes-home'],{encoding:'utf8'}));
  const deadline=Date.now()+90000;let rows=[];
  while(Date.now()<deadline){rows=verify();if(taskIds.every(id=>rows.some(r=>r.id===id&&r.status==='archived')))break;await new Promise(r=>setTimeout(r,500));}
  assert.ok(taskIds.every(id=>rows.some(r=>r.id===id&&r.status==='archived')),'test tasks were not archived');
  outcome.cleanup={job:cleanup.id,tasks:rows};console.log('PASS exact test-task cleanup readback',JSON.stringify(outcome.cleanup));
 }
 const outfile=process.env.Z0LIVE_TEST_OUTPUT;
 if(outfile)fs.writeFileSync(outfile,JSON.stringify(outcome,null,2),{mode:0o600});
} finally {
 await browser.close();
 await request('/v1/sessions/'+sid+'/end',{});
 // Revoke this harness's disposable bearer after collecting exact device identity.
 execFileSync('python3',['-c',`import sys;sys.path.insert(0,${JSON.stringify(path.join(root,'desktop'))});from z0live_desktop.core import Store;s=Store();s.revoke_token(sys.stdin.read())`],{input:token});
}

// Opt-in real GPT WebRTC/native stack verification, isolated broker recommended.
// No microphone/audio recording, no credentials or private output in stdout.
import {chromium} from 'playwright-core';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
const root=path.resolve(import.meta.dirname,'../..');
const base=process.env.Z0LIVE_TEST_BASE||'http://127.0.0.1:18791';
const pairing=execFileSync(path.join(root,'bin/z0live'),['pair','voice-stack-test-'+Date.now()],{encoding:'utf8'});
const token=pairing.match(/Device token: ([^\n]+)/)[1];
const request=async(p,body)=>{const r=await fetch(base+p,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok)throw new Error(p+' HTTP '+r.status);return r.json();};
const session=await request('/v1/realtime/session',{client:'web',context:{app:'isolated-voice-stack-qualification',test:true},tools:[]});
assert.ok(session.voice_plan?.digest,'z0intelligence VoicePlan missing');
const sid=session.session_id;
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
let outcome;
try{
 const page=await browser.newPage();
 await page.route(base+'/qualification-stack',r=>r.fulfill({contentType:'text/html',body:'<!doctype html><title>Isolated voice stack test</title>'}));
 await page.goto(base+'/qualification-stack');
 outcome=await page.evaluate(async({base,token,session})=>{
  const sid=session.session_id;
  const req=async(p,b)=>{const r=await fetch(base+p,{method:b===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)});if(!r.ok)throw new Error(p+' HTTP '+r.status);return r.json();};
  let seq=0,floor='quiet';
  const event=(type,text='',data={})=>req('/v1/events',[{session_id:sid,type,text,data:{...data,event_id:'stack-'+(++seq)}}]);
  const setFloor=async(state,barge_in=false)=>{floor=state;await event('floor','',{state,barge_in});};
  const pc=new RTCPeerConnection();pc.addTransceiver('audio',{direction:'recvonly'});
  const dc=pc.createDataChannel('oai-events'),send=e=>dc.send(JSON.stringify(e));
  let current=null,active=false,creating=false,pending=0,continuation=false,registered=[];
  const errors=[],turns=[],inbox=[],calls=[];let cursor=0;
  const create=()=>{creating=true;send({type:'response.create',response:{output_modalities:['text']}});};
  const advance=()=>{if(continuation&&!active&&!creating&&!pending){continuation=false;create();}};
  dc.onmessage=async({data})=>{
   const e=JSON.parse(data);
   if(e.type==='session.created')registered=e.session.tools.map(t=>t.name);
   if(e.type==='error'){errors.push(e.error);current?.reject(new Error('provider '+e.error.code));}
   if(e.type==='response.created'){creating=false;active=true;await setFloor('assistant');}
   if(e.type==='response.output_text.delta'&&current&&current.first_text_ms===null)current.first_text_ms=Math.round(performance.now()-current.started);
   if(e.type==='response.function_call_arguments.done'){
    pending++;const started=performance.now();
    try{
     const args=JSON.parse(e.arguments);
     await event('tool_call',e.name,{name:e.name,call_id:e.call_id,arguments:args});
     const result=await req('/v1/tools/'+e.name,{session_id:sid,call_id:e.call_id,arguments:args});
     const call={name:e.name,ms:Math.round(performance.now()-started),result};calls.push(call);current?.calls.push(call);
     await event('tool_result',JSON.stringify(result).slice(0,14000),{name:e.name,call_id:e.call_id,success:!!result.ok});
     send({type:'conversation.item.create',item:{type:'function_call_output',call_id:e.call_id,output:JSON.stringify(result)}});
     continuation=true;
    }catch(error){current?.reject(error);}finally{pending--;advance();}
   }
   if(e.type==='response.output_text.done'){current?.outputs.push(e.text);await event('assistant_transcript',e.text);}
   if(e.type==='response.done'){
    active=false;
    const hasFunction=(e.response.output||[]).some(o=>o.type==='function_call');
    if(current)current.usage.push({response_id:e.response.id,usage:e.response.usage});
    advance();
    if(!hasFunction&&!active&&!creating&&!pending&&!continuation&&current?.outputs.length){
     await setFloor('quiet');current?.resolve();
    }
   }
  };
  const opened=new Promise(r=>dc.onopen=r);
  const offer=await pc.createOffer();await pc.setLocalDescription(offer);
  const sdp=await fetch('https://api.openai.com/v1/realtime/calls',{method:'POST',headers:{Authorization:'Bearer '+session.value,'Content-Type':'application/sdp'},body:offer.sdp});
  if(sdp.status!==201)throw new Error('SDP HTTP '+sdp.status);
  await pc.setRemoteDescription({type:'answer',sdp:await sdp.text()});
  await Promise.race([opened,new Promise((_,r)=>setTimeout(()=>r(new Error('data-channel timeout')),30000))]);
  send({type:'session.update',session:{type:'realtime',output_modalities:['text']}});
  const heartbeat=setInterval(()=>event('app_state','',{test:true,floor}).catch(()=>{}),20000);
  const poll=async()=>{const d=await req('/v1/inbox?after='+cursor);cursor=d.after;inbox.push(...d.messages.filter(m=>m.session_id===sid));};
  const trace=async(id)=>(await req('/v1/trace?turn_id='+id)).events;
  async function utterance(text){
   const started=performance.now();
   const record={utterance:text,started,first_text_ms:null,outputs:[],calls:[],usage:[]};
   const done=new Promise((resolve,reject)=>current={...record,resolve,reject});
   await setFloor('user');await event('user_transcript',text);
   send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text}]}});
   create();
   let timer;try{await Promise.race([done,new Promise((_,r)=>timer=setTimeout(()=>r(new Error('voice turn timeout')),60000))]);}finally{clearTimeout(timer);}
   const t={...record,first_text_ms:current.first_text_ms,ms:Math.round(performance.now()-started)};
   turns.push(t);current=null;
   const id=t.calls.find(c=>c.result.turn_id)?.result.turn_id;
   if(id)for(const u of t.usage)if(u.usage)await event('model_usage','',{turn_id:id,...u,ms:t.ms});
   return {record:t,id};
  }
  async function answer(id,timeout=180000){
   const start=performance.now(),end=Date.now()+timeout;
   while(Date.now()<end){await poll();const result=inbox.find(m=>m.params.turn_id===id&&['result','clarification'].includes(m.params.event));if(result)return {result,answer_wait_ms:Math.round(performance.now()-start),trace:await trace(id)};await new Promise(r=>setTimeout(r,300));}
   throw new Error('async answer timeout '+id+' '+JSON.stringify(await trace(id)));
  }
  try{
   const quick=await utterance('What model are you using? Ask ask_z0 so the liaison answers from its pinned session cache.');
   quick.trace=await trace(quick.id);
   const memory=await utterance('What did we decide about z0intelligence running in a separate app? Use ask_z0 to check our memory, and do not invent an answer if the decision is missing.');
   Object.assign(memory,await answer(memory.id));
   const fanout=await utterance('Summarize all open PRs across kvnloo/lawnchair and kvnloo/z0live and which need my decision. Use ask_z0; have the meta agent use at least two cheaper context workers, one per repository, and verify pagination. Read-only, do not edit or post anything.');
   Object.assign(fanout,await answer(fanout.id));
   const interrupted=await utterance('[test] Summarize open PRs across kvnloo/lawnchair and kvnloo/z0live with two cheaper native workers, one per repo. This is a cancellation test: each worker should first run a harmless 20 second python sleep with explicit workdir, then its read-only gh lookup. Do not edit or publish. Use ask_z0.');
   const deadline=Date.now()+90000;
   while(Date.now()<deadline){const t=await trace(interrupted.id);if(t.filter(e=>e.layer==='L3'&&e.event==='started').length>=2)break;await poll();await new Promise(r=>setTimeout(r,250));}
   interrupted.before=await trace(interrupted.id);
   if(interrupted.before.filter(e=>e.layer==='L3'&&e.event==='started').length<2)throw new Error('two workers never started');
   const cancelStart=performance.now();await setFloor('user',true);
   await utterance('Never mind, stop that work and its workers. Use cancel_work.');
   const until=Date.now()+60000;
   while(Date.now()<until){const t=await trace(interrupted.id);if(t.some(e=>e.layer==='L1'&&e.event==='cancelled'))break;await new Promise(r=>setTimeout(r,250));}
   interrupted.cancel_ms=Math.round(performance.now()-cancelStart);
   interrupted.trace=await trace(interrupted.id);await poll();
   return {session_id:sid,plan:session.voice_plan,registered,errors,quick,memory,fanout,interrupted,turns,inbox};
  }finally{clearInterval(heartbeat);pc.close();}
 },{base,token,session});
 if(process.env.Z0LIVE_TEST_OUTPUT)fs.writeFileSync(process.env.Z0LIVE_TEST_OUTPUT,JSON.stringify(outcome,null,2),{mode:0o600});
 assert.deepEqual(outcome.errors,[]);
 assert.ok(outcome.quick.trace.some(e=>e.layer==='L1'&&e.event==='cache_hit'));
 assert.ok(outcome.fanout.trace.filter(e=>e.layer==='L3'&&e.event==='started').length>=2);
 assert.ok(outcome.interrupted.trace.some(e=>e.layer==='L1'&&e.event==='cancelled'));
 const childIds=new Set(outcome.interrupted.before.filter(e=>e.layer==='L3'&&e.event==='started').map(e=>e.details.run_id));
 const stopped=new Set(outcome.interrupted.trace.filter(e=>e.layer==='L3'&&((e.event==='cancel_confirmed'&&['cancelled','interrupted'].includes(e.details.native_status))||['cancelled','interrupted'].includes(e.event))).map(e=>e.details.run_id));
 assert.ok([...childIds].every(id=>stopped.has(id)),'native child cancellation confirmation missing');
 console.log('PASS real WebRTC stack',JSON.stringify({session_id:sid,models:outcome.plan.roles,turns:outcome.turns.map(t=>({first_text_ms:t.first_text_ms,ms:t.ms,calls:t.calls.map(c=>({name:c.name,ms:c.ms,id:c.result.turn_id}))})),layers:[outcome.memory,outcome.fanout,outcome.interrupted].map(t=>({id:t.id,cancel_ms:t.cancel_ms,trace:t.trace.filter(e=>['briefed','condensed','completed','cancelled','cache_hit'].includes(e.event)).map(e=>({layer:e.layer,event:e.event,model:e.model,ms:e.ms,usage:e.usage,cost_usd:e.cost_usd}))}))}));
}finally{
 await browser.close();
 await request('/v1/sessions/'+sid+'/end',{});
 execFileSync('python3',['-c',`import sys;sys.path.insert(0,${JSON.stringify(path.join(root,'desktop'))});from z0live_desktop.core import Store;Store().revoke_token(sys.stdin.read())`],{input:token});
}

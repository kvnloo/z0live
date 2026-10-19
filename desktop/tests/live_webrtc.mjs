// Opt-in REAL provider/WebRTC/Hermes test. Never prints tokens or ephemeral values.
import {chromium} from 'playwright-core';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

const root=path.resolve(import.meta.dirname,'../..');
const base=process.env.Z0LIVE_TEST_BASE||'https://0.taila30dc.ts.net:8791';
const pairing=execFileSync(path.join(root,'bin/z0live'),['pair','headless-live-'+Date.now()],{encoding:'utf8'});
const token=pairing.match(/Device token: ([^\n]+)/)[1];
const request=async(p,body)=>{
 const r=await fetch(base+p,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 if(!r.ok) throw new Error(`${p} HTTP ${r.status}: ${await r.text()}`);
 return r.json();
};
const health=await request('/v1/health');assert.equal(health.ok,true);console.log('PASS health',JSON.stringify(health));
const unauth=await fetch(base+'/v1/health');assert.equal(unauth.status,401);console.log('PASS missing-token rejection',unauth.status);
const firstInbox=await request('/v1/inbox?after=0');
let cursor=firstInbox.after;
const session=await request('/v1/realtime/session',{client:'web',context:{app:'headless-qualification',screen:'text-webrtc'},tools:[{type:'function',name:'local_test','description':'Harmless client-local qualification tool','parameters':{type:'object','properties':{},'additionalProperties':false}}]});
assert.ok(session.value.length>10);
console.log('PASS real ephemeral mint',JSON.stringify({secret_length:session.value.length,expires_at:session.expires_at,model:session.model,voice:session.voice,instructions_version:session.instructions_version,session_id:session.session_id}));
const sid=session.session_id,marker='Z0LIVE_E2E_'+Date.now();
const utterance=`This is a [test] connectivity check. Use tell_hermes now with kind praise and text: [test] Reply exactly ${marker}. Do not use other tools or edit files.`;
await request('/v1/events',[{session_id:sid,ts:Date.now()/1000,type:'user_transcript',text:utterance,data:{event_id:marker+'-user'}}]);
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage();
await page.route(base+'/qualification',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>z0live real WebRTC qualification</title>'}));
let outcome;
try {
 await page.goto(base+'/qualification');
 outcome=await page.evaluate(async({base,token,session,utterance,marker})=>{
   const sid=session.session_id;
   const req=async(p,body)=>{const r=await fetch(base+p,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('broker HTTP '+r.status);return r.json();};
   const pc=new RTCPeerConnection();
   pc.addTransceiver('audio',{direction:'recvonly'});
   const dc=pc.createDataChannel('oai-events');
   const events=[],calls=[],outputs=[],errors=[];
   let active=false,continuation=false,toolDone=false,toolPending=0;
   const send=e=>dc.send(JSON.stringify(e));
   const continueWhenSafe=()=>{if(continuation&&!active&&!toolPending){continuation=false;send({type:'response.create',response:{output_modalities:['text']}});}};
   let resolveDone,rejectDone;
   const done=new Promise((resolve,reject)=>{resolveDone=resolve;rejectDone=reject;});
   const timer=setTimeout(()=>rejectDone(new Error('Realtime text/tool timeout; '+JSON.stringify({events:events.slice(-12),calls,errors}))),60000);
   dc.onmessage=async({data})=>{
     const e=JSON.parse(data);events.push(e.type);
     if(e.type==='error'){errors.push(e.error?.code);return;}
     if(e.type==='response.created')active=true;
     if(e.type==='session.created'){
       window.__registered=e.session.tools.map(t=>t.name);
     }
     if(e.type==='response.function_call_arguments.done'){
       toolPending++;
       try{
         const args=JSON.parse(e.arguments);calls.push(e.name);
         await req('/v1/events',[{session_id:sid,ts:Date.now()/1000,type:'tool_call',text:e.name,data:{event_id:e.call_id+'-call',name:e.name,arguments:args,call_id:e.call_id}}]);
         const result=await req('/v1/tools/'+e.name,{session_id:sid,arguments:args});
         await req('/v1/events',[{session_id:sid,ts:Date.now()/1000,type:'tool_result',text:'queued',data:{event_id:e.call_id+'-result',name:e.name,call_id:e.call_id,result,success:true}}]);
         send({type:'conversation.item.create',item:{type:'function_call_output',call_id:e.call_id,output:JSON.stringify(result)}});
         toolDone=true;continuation=true;
       }catch(err){rejectDone(err);}finally{toolPending--;continueWhenSafe();}
     }
     if(e.type==='response.output_text.done'){
       outputs.push(e.text);
       await req('/v1/events',[{session_id:sid,ts:Date.now()/1000,type:'assistant_transcript',text:e.text,data:{event_id:e.response_id+'-assistant'}}]);
     }
     if(e.type==='response.done'){
       active=false;
       if(e.response.status!=='completed')rejectDone(new Error('response status '+e.response.status));
       if(toolDone&&!continuation&&!toolPending&&outputs.length)resolveDone();
       continueWhenSafe();
     }
   };
   const open=new Promise(resolve=>dc.onopen=resolve);
   const offer=await pc.createOffer();await pc.setLocalDescription(offer);
   const answer=await fetch('https://api.openai.com/v1/realtime/calls',{method:'POST',headers:{Authorization:'Bearer '+session.value,'Content-Type':'application/sdp'},body:offer.sdp});
   const sdpStatus=answer.status;if(!answer.ok)throw new Error('SDP HTTP '+sdpStatus);
   await pc.setRemoteDescription({type:'answer',sdp:await answer.text()});
   await Promise.race([open,new Promise((_,reject)=>setTimeout(()=>reject(new Error('data channel timeout')),30000))]);
   // Set text mode after the real data channel opens; this avoids recording the host microphone.
   send({type:'session.update',session:{type:'realtime',output_modalities:['text']}});
   send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:utterance}]}});
   send({type:'response.create',response:{output_modalities:['text']}});
   try{await done;}finally{clearTimeout(timer);pc.close();}
   return {sdp_status:sdpStatus,calls,assistant_chars:outputs.join('').length,errors,registered_tools:window.__registered,event_count:events.length};
 },{base,token,session,utterance,marker});
 assert.equal(outcome.sdp_status,201);assert.ok(outcome.calls.includes('tell_hermes'));assert.ok(outcome.registered_tools.includes('local_test'));assert.deepEqual(outcome.errors,[]);
 console.log('PASS real headless Chromium WebRTC + GPT tell_hermes',JSON.stringify(outcome));
} finally {await browser.close();}
const end=await request('/v1/sessions/'+sid+'/end',{});console.log('PASS session end / service-generated summary queued',JSON.stringify(end));
let reply,summaryReply;
const deadline=Date.now()+180000;
while(Date.now()<deadline){
 const inbox=await request('/v1/inbox?after='+cursor);cursor=inbox.after;
 assert.ok(inbox.messages.filter(m=>m.session_id===sid).every(m=>m.say===''),'ended session spoke backlog');
 // Notes and summaries must be recorded natively, NOT echoed through phone TTS.
 const data=process.env.Z0LIVE_DATA||path.resolve(root,'../../data/z0live');
 const receipts=JSON.parse(execFileSync('python3',['-c',`import json,sqlite3,sys,pathlib; db=sqlite3.connect((pathlib.Path(sys.argv[1])/'broker.db').as_uri()+'?mode=ro',uri=True); db.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in db.execute("SELECT id,kind,status,run_id,result FROM jobs WHERE session_id=? ORDER BY id",(sys.argv[2],))]))`,data,sid],{encoding:'utf8'}));
 reply=receipts.find(r=>r.kind==='feedback_note'&&r.status==='completed'&&(r.result||'').includes(marker));
 const summary=receipts.find(r=>r.kind==='summary');
 if(reply&&summary?.status==='completed'){summaryReply=summary;break;}
 await new Promise(resolve=>setTimeout(resolve,1500));
}
assert.ok(reply,'real native note reply was not recorded');assert.ok(summaryReply,'native summary did not complete');
console.log('PASS native note + summary recorded, never spoken',JSON.stringify({note_job:reply.id,summary_job:summaryReply.id}));
const sessions=await request('/v1/sessions');assert.ok(sessions.sessions.some(s=>s.id===sid&&s.ended));
const controller=new AbortController();
const stream=await fetch(base+'/v1/stream?after='+cursor,{headers:{Authorization:'Bearer '+token},signal:controller.signal});
assert.equal(stream.status,200);const reader=stream.body.getReader();const chunk=await reader.read();controller.abort();
assert.ok(new TextDecoder().decode(chunk.value).includes(': z0live connected'));console.log('PASS authenticated SSE connection, no audible backlog');
console.log('LIVE RESULT',JSON.stringify({base,session_id:sid,hermes_marker:marker,summary_run:summaryReply.run_id,note_run:reply.run_id}));

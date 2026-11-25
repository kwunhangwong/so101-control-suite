import {updateCleaningAvailability} from './cleaning.js';
import {cropRect} from './roi.mjs';
const $=id=>document.getElementById(id),sessionId=crypto.randomUUID();
let state=null,online=false,busy=false,closed=false,clientError=null,showChecks=false,collectionMode='seed';
let lastLog='';
const cameras=Object.fromEntries(['front','wrist'].map(name=>[name,{roi:null,error:null}]));
const labels={offline:'OFFLINE',connecting:'CONNECTING',ready:'READY',holding:'HOLDING',starting:'STARTING',resetting:'RESETTING',following:'FOLLOWING',evaluating:'EVALUATING',bootstrapping:'GPT BOOTSTRAP',calibrating:'RE-CALIBRATING',releasing:'RELEASING',error:'CHECK NEEDED'};
function render(next){
  state=next;
  const requestBusy=busy;busy=busy||next.bootstrap_cleaning?.busy||next.bootstrap_cleaning?.pending;
  const moving=['starting','following','evaluating','bootstrapping','resetting'].includes(next.phase);
  const blocked=next.bootstrap?.episode?.status==='awaiting_label'||next.evaluation?.episode?.status==='awaiting_label'||!online||busy||moving||next.checking_connections||next.calibration_busy||next.gripper_busy||Boolean(next.recalibration)||['connecting','releasing'].includes(next.phase);
  $('check-connections').disabled=blocked;
  $('check-connections').textContent=next.checking_connections?'Checking…':'Check Connection';
  $('reset').disabled=blocked||!next.reset_pose;
  $('stop').hidden=!moving;$('stop').disabled=!online||!moving;
  $('mode').textContent=online?labels[next.phase]||next.phase:'OFFLINE';$('mode').className=`badge ${online?next.phase:''}`;
  const checks=showChecks?next.connections||{}:{};
  for(const row of document.querySelectorAll('[data-device]')){
    const result=checks[row.dataset.device],connected=online&&result?.connected===true;
    row.classList.toggle('passed',connected);row.querySelector('.symbol').textContent=connected?'✓':'✗';
    row.querySelector('.detail').textContent=!online?'Not checked':result?(connected?'Connected':result.error||'Not connected'):next.checking_connections?'Checking…':'Not checked';
  }
  $('connection-summary').textContent=next.checking_connections?'Checking both cameras and both arms…':showChecks?`${online?Object.values(checks).filter(r=>r.connected).length:0}/4 devices connected. Check again after changing USB connections.`:'Not checked. Check all four devices before starting.';
  const error=clientError||next.error||next.reset_pose_error;
  $('alert').hidden=!error;$('alert').textContent=error||'';
  $('message').textContent=online?next.message:'Connection lost. Reconnecting…';
  renderLog(next);
  renderEpisode(next);
  updateCleaningAvailability(next,requestBusy);
  busy=requestBusy;
}
function renderLog(next){
  const error=clientError||next.error||next.evaluation?.error||next.bootstrap?.error;
  $('log-status').textContent=!online?'OFFLINE':error?'ERROR':'LIVE';
  $('log-status').className=`badge ${!online?'':error?'error':'live'}`;
  $('log-current-error').hidden=!error;$('log-current-error').textContent=error||'';
  const warnings=Object.values(next.warnings||{});
  $('log-warnings').hidden=!warnings.length;$('log-warnings').textContent=warnings.join(' · ');
  const events=next.events||[],signature=JSON.stringify(events);
  if(signature===lastLog)return;
  lastLog=signature;
  const log=$('live-log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
  log.replaceChildren(...events.map(event=>{
    const row=document.createElement('div');row.className=`log-row ${event.level.toLowerCase()}`;
    const time=document.createElement('time');time.dateTime=event.time;time.textContent=event.time.slice(11,23);
    const level=document.createElement('strong');level.textContent=event.level;
    const message=document.createElement('span');message.textContent=event.message;
    row.append(time,level,message);return row;
  }));
  if(atBottom)log.scrollTop=log.scrollHeight;
}
async function command(action,confirm,extra={}){
  busy=true;clientError=null;if(state)render(state);
  try{
    const response=await fetch(`/api/${action}?session=${encodeURIComponent(sessionId)}`,{method:'POST',headers:confirm?{'Content-Type':'application/json'}:{},body:confirm?JSON.stringify({confirm,...extra}):undefined});
    const result=await response.json();if(!response.ok)throw new Error(result.error);
    if(action==='check-connections')showChecks=true;
    render(result);
  }catch(error){clientError=error.message;}
  finally{busy=false;if(state)render(state);}
}
function cameraError(name,message){
  $(`${name}-frame`).hidden=true;$(`${name}-placeholder`).hidden=false;$(`${name}-placeholder`).textContent=message;
  $(`${name}-status`).textContent='UNAVAILABLE';$(`${name}-status`).className='badge error';
}
async function loadRois(){
  await Promise.all(Object.entries(cameras).map(async([name,camera])=>{
    try{
      const response=await fetch(`/api/${name}/roi`,{cache:'no-store'}),roi=await response.json();
      if(!response.ok)throw new Error(roi.error);
      cropRect(roi,...roi.source_size_wh);
      camera.roi=roi;camera.error=null;
    }catch(error){camera.roi=null;camera.error=error.message;cameraError(name,error.message);}
  }));
  if(!closed)setTimeout(loadRois,2000);
}
async function frame(name){
  const camera=cameras[name];
  try{
    if(!$('clean-view').hidden)return;
    if(!online)throw new Error('Waiting for the app connection…');
    if(!camera.roi)throw new Error(camera.error||'Loading saved ROI…');
    const response=await fetch(`/api/${name}/frame`,{cache:'no-store',signal:AbortSignal.timeout(2500)});
    if(!response.ok)throw new Error((await response.json()).error);
    const image=await createImageBitmap(await response.blob());
    try{
      if(closed)return;
      if(!camera.roi)throw new Error(camera.error||'Loading saved ROI…');
      const episode=state?.bootstrap?.episode?.status==='recording'?state.bootstrap.episode:state?.evaluation?.episode?.status==='recording'?state.evaluation.episode:state?.episode;
      const roi=episode?.status==='recording'?episode.cameras[name].roi:camera.roi;
      const [x,y,w,h]=cropRect(roi,image.width,image.height),canvas=$(`${name}-frame`);
      canvas.width=w;canvas.height=h;canvas.getContext('2d').drawImage(image,x,y,w,h,0,0,w,h);
      canvas.hidden=false;$(`${name}-placeholder`).hidden=true;
      $(`${name}-status`).textContent='LIVE';$(`${name}-status`).className='badge live';
      $(`${name}-roi`).textContent=`${w} × ${h} crop · [${x}, ${y}, ${x+w}, ${y+h}] on ${image.width} × ${image.height} · camera_setup/config/${name}_roi.json`;
    }finally{image.close();}
  }catch(error){cameraError(name,error.message);}
  finally{if(!closed)setTimeout(()=>frame(name),100);}
}
function renderEpisode(next){
  const episode=next.episode||{status:'idle'},recording=episode.status==='recording',pending=episode.status==='awaiting_label';
  const finishing=episode.robot_finished===false&&!recording;
  const fresh=[next.camera,next.wrist_camera].every(camera=>camera?.state==='live'&&Date.now()/1000-camera.captured_at<1);
  if(episode.context){
    for(const [id,key] of [['instruction','instruction'],['success-condition','success_condition']]){
      if(!inputHistory[id].length&&!$(id).value&&episode.context[key]){
        $(id).value=episode.context[key];
        localStorage.setItem(`so101-seed-${id}`,$(id).value);
        rememberInput(id);
      }
    }
  }
  const validTask=$('instruction').value.trim()&&$('success-condition').value.trim();
  $('episode-start').disabled=collectionMode!=='seed'||!online||busy||recording||pending||!fresh||!validTask||!next.connected||!next.calibration_ok||Boolean(next.error)||next.checking_connections||next.gripper_busy||Boolean(next.recalibration)||!['ready','holding'].includes(next.phase);
  $('episode-end').disabled=!online||busy||!recording;
  $('instruction').disabled=recording||pending;$('success-condition').disabled=recording||pending;
  for(const id of ['instruction','success-condition'])$(id+'-history').disabled=recording||pending||!inputHistory[id].length;
  if((recording||pending)&&episode.context){$('instruction').value=episode.context.instruction||'';$('success-condition').value=episode.context.success_condition||'';}
  $('episode-status').textContent=recording?'RECORDING':finishing?'STOPPING':pending?'LABEL REQUIRED':episode.status==='saved'?episode.label.toUpperCase():'READY';
  $('episode-status').className=`badge ${recording?'recording-active':pending?'error':episode.status==='saved'?'ready':''}`;
  $('episode-progress').textContent=episode.path?`${episode.frames} camera pairs · ${(episode.duration_seconds||0).toFixed(1)} s · ${recording?'Recording both cameras and robot commands':finishing?'Stopping and saving joint data…':pending?'Recording ended — choose a label':`Saved as ${episode.label}`}`:fresh?'Enter the task and success condition, then start.':'Waiting for both cameras to provide fresh frames.';
  $('episode-labels').hidden=!pending;
  for(const id of ['episode-success','episode-failure'])$(id).disabled=!online||busy||finishing;
  $('episode-path').textContent=episode.path||'No episode saved yet.';
  $('episode-error').hidden=!episode.error;$('episode-error').textContent=episode.error||'';
  renderCollectionModes(next);
}
function renderCollectionModes(){
  for(const button of document.querySelectorAll('[data-collection-mode]')){
    button.disabled=button.dataset.collectionMode!=='seed';
  }
}
const inputHistory={};
function rememberInput(id){
  const value=$(id).value.trim();
  if(!value)return;
  inputHistory[id]=[value,...inputHistory[id].filter(entry=>entry!==value)].slice(0,5);
  localStorage.setItem(`so101-seed-${id}-history`,JSON.stringify(inputHistory[id]));
  renderInputHistory(id);
}
function renderInputHistory(id){
  const select=$(id+'-history'),label=id==='instruction'?'task instructions':'success conditions';
  select.replaceChildren(new Option(inputHistory[id].length?`Recent ${label} (${inputHistory[id].length})`:`No recent ${label} yet`,''));
  for(const value of inputHistory[id])select.add(new Option(value,value));
  select.disabled=$(id).disabled||!inputHistory[id].length;
}
for(const id of ['instruction','success-condition']){
  const input=$(id),select=$(id+'-history');
  input.value=localStorage.getItem(`so101-seed-${id}`)||'';
  let saved=[];
  try{saved=JSON.parse(localStorage.getItem(`so101-seed-${id}-history`)||'[]');}catch{}
  inputHistory[id]=Array.isArray(saved)?[...new Set(saved.filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim()))].slice(0,5):[];
  // Keep the previous version's latest field value when upgrading.
  if(!inputHistory[id].length)rememberInput(id);
  renderInputHistory(id);
  input.addEventListener('input',()=>{localStorage.setItem(`so101-seed-${id}`,input.value);if(state)render(state);});
  input.addEventListener('change',()=>rememberInput(id));
  select.addEventListener('change',()=>{
    if(!select.value||input.disabled)return;
    input.value=select.value;
    localStorage.setItem(`so101-seed-${id}`,input.value);
    rememberInput(id);
    input.focus();
    if(state)render(state);
  });
}
$('episode-start').addEventListener('click',()=>{
  if(collectionMode!=='seed')return;
  for(const id of ['instruction','success-condition'])rememberInput(id);
  command('episode-start','RECORD',{instruction:$('instruction').value.trim(),success_condition:$('success-condition').value.trim()});
});
$('episode-end').addEventListener('click',()=>command('episode-end'));
$('episode-success').addEventListener('click',()=>command('episode-label','LABEL',{label:'success'}));
$('episode-failure').addEventListener('click',()=>command('episode-label','LABEL',{label:'failure'}));
$('check-connections').addEventListener('click',()=>{showChecks=false;command('check-connections');});
$('reset').addEventListener('click',()=>command('reset','RESET'));
$('stop').addEventListener('click',()=>command('stop'));
const stream=new EventSource(`/api/session?session=${encodeURIComponent(sessionId)}`);
stream.onmessage=event=>{online=true;render(JSON.parse(event.data));};
stream.onerror=()=>{online=false;if(state)render(state);for(const name of Object.keys(cameras))cameraError(name,'Connection lost. Waiting for fresh frames…');};
window.addEventListener('pagehide',()=>{closed=true;stream.close();navigator.sendBeacon(`/api/session/close?session=${encodeURIComponent(sessionId)}`);for(const id of ['instruction','success-condition'])if(!$(id).disabled)rememberInput(id);});
window.addEventListener('pageshow',event=>{if(event.persisted&&closed)location.reload();});
loadRois();for(const name of Object.keys(cameras))frame(name);

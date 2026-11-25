const $=id=>document.getElementById(id);
const sessionId=crypto.randomUUID();
let state=null,online=false,busy=false,closed=false,clientError=null,showConnectionResults=false;
let checkpointList=null;
let eventList=null;
let forceReadAt=null;
let releaseRequested=new URLSearchParams(location.search).get('release')==='1';
const labels={offline:'OFFLINE',connecting:'CONNECTING',ready:'READY',holding:'HOLDING',starting:'STARTING',resetting:'RESETTING',following:'FOLLOWING',evaluating:'EVALUATING',calibrating:'RE-CALIBRATING',releasing:'RELEASING',error:'CHECK NEEDED'};
function render(next){
  state=next;
  const moving=['starting','following','evaluating','resetting'].includes(next.phase),calibrating=Boolean(next.recalibration),transition=next.phase==='connecting'||next.phase==='releasing'||next.checking_connections||next.calibration_busy;
  const checks=showConnectionResults?next.connections||{}:{};
  for(const row of document.querySelectorAll('[data-device]')){
    const result=checks[row.dataset.device],connected=online&&result?.connected===true;
    row.classList.toggle('passed',connected);row.classList.toggle('failed',Boolean(result)&&!connected);
    row.querySelector('.check-symbol').textContent=connected?'✓':'✗';
    const detail=!online?'Not checked':result?(connected?'Connected':result.error||'Not connected'):showConnectionResults&&next.checking_connections?'Checking…':'Not checked';
    row.querySelector('.check-detail').textContent=detail;row.title=detail;
  }
  $('connection-summary').textContent=next.checking_connections?'Checking both cameras and both arms…':showConnectionResults?`${online?Object.values(checks).filter(r=>r.connected).length:0}/4 devices connected. Check again after changing USB connections.`:'Not checked. Check all four devices before starting.';
  $('check-connections').textContent=next.checking_connections?'Checking…':'Check Connection';
  $('check-connections').disabled=!online||busy||moving||transition||calibrating;
  $('mode').textContent=online?labels[next.phase]||next.phase:'OFFLINE';
  $('mode').className=`badge ${next.phase}`;
  const worst=next.joints.reduce((largest,joint)=>!largest||Math.abs(joint.error)>Math.abs(largest.error)?joint:largest,null);
  const max=worst?Math.abs(worst.error):null;
  $('teach-rest').disabled=!online||busy||moving||transition||calibrating||!next.calibration_ok||Boolean(next.error);
  $('reset').disabled=!online||busy||moving||transition||calibrating||!next.reset_pose;
  $('test').disabled=!online||busy||moving||transition||calibrating;
  const testBlocker=!online?'Waiting for the app connection.':
    busy||moving||transition||calibrating?'Finish or stop the current operation first.':
    next.error?`Resolve the reported error, then use Check Connection: ${next.error}`:
    !next.connected||!next.calibration_ok?'Use Check Connection to connect both arms and verify their calibration.':
    next.joints.length!==6||max===null||!next.joints.every(j=>Number.isFinite(j.error))?'Waiting for readings from all six joints.':'';
  const gapWarning=max>30?`${worst.name[0].toUpperCase()+worst.name.slice(1)} starting gap: ${max.toFixed(1)}°. Warning only: starting can cause a sudden move. You can still start the test.`:'';
  $('test-readiness').textContent=testBlocker||gapWarning||`Ready. Largest starting gap: ${max.toFixed(1)}°. Motion checks give warnings only.`;
  $('test-readiness').classList.toggle('blocked',Boolean(testBlocker||gapWarning));
  $('confirm-test').disabled=Boolean(testBlocker);
  $('recalibrate').disabled=!online||busy||moving||transition||calibrating;
  renderCalibration(next);
  renderCheckpoints(next);
  renderLog(next);
  renderGripper(next);
  $('alignment-summary').textContent=moving?next.message:next.reset_pose_error||'Clear Error & Reset slowly returns to the saved resting pose at up to 10°/s, with a gentle start and stop. Clear the path and remove any gripper obstruction first. Stop & hold remains available.';
  $('stop').hidden=!moving;
  $('stop').disabled=!online||!moving;
  $('message').textContent=online?next.message:'Connection lost. Reconnecting…';
  if(releaseRequested&&online&&next.connected&&!transition&&!calibrating){
    if(moving){clientError='Choose Stop & hold before releasing torque. Support the follower arm before confirming release.';}
    else{releaseRequested=false;clientError=null;history.replaceState(null,'',location.pathname);if(Object.values(next.torque).some(Boolean)){$('supported').checked=false;$('confirm-release').disabled=true;$('release-dialog').showModal();}}
  }
  $('alert').hidden=!(clientError||next.error);
  $('alert').textContent=clientError||next.error||'';
}
async function command(action,confirm,data={}){
  busy=true;clientError=null;if(state)render(state);
  try{
    const response=await fetch(`/api/${action}?session=${encodeURIComponent(sessionId)}`,{method:'POST',headers:confirm?{'Content-Type':'application/json'}:{},body:confirm?JSON.stringify({confirm,...data}):undefined});
    const result=await response.json();if(!response.ok)throw new Error(result.error);
    if(action==='check-connections')showConnectionResults=true;
    render(result);
  }catch(error){clientError=error.message;}
  finally{busy=false;if(state)render(state);}
}
$('check-connections').addEventListener('click',()=>{showConnectionResults=false;command('check-connections');});
$('teach-rest').addEventListener('click',()=>command('teach-rest','TEACH_REST'));
$('reset').addEventListener('click',()=>command('reset','RESET'));
$('test').addEventListener('click',()=>$('test-dialog').showModal());
$('confirm-test').addEventListener('click',()=>{if(!$('confirm-test').disabled){$('test-dialog').close();command('test','TEST');}});
$('recalibrate').addEventListener('click',()=>{$('calibration-supported').checked=false;renderCheckpoints(state);$('recalibrate-dialog').showModal();});
$('gripper-force').addEventListener('click',()=>{forceReadAt=null;$('gripper-dialog').showModal();command('gripper-read');});
$('force-slider').addEventListener('input',()=>{$('force-percent').value=$('force-slider').value;renderGripper(state);});
$('force-percent').addEventListener('input',()=>{$('force-slider').value=$('force-percent').value;renderGripper(state);});
$('apply-force').addEventListener('click',()=>{if(!$('apply-force').disabled)command('gripper-force','GRIPPER_FORCE',{percent:Number($('force-percent').value)});});
$('force-stop').addEventListener('click',()=>{$('gripper-dialog').close();command('stop');});
$('calibration-supported').addEventListener('change',()=>renderCheckpoints(state));
$('calibration-checkpoint').addEventListener('change',()=>renderCheckpoints(state));
$('restore-calibration').addEventListener('click',()=>{if(!$('restore-calibration').disabled){$('recalibrate-dialog').close();command('recalibrate-restore','RESTORE_CALIBRATION',{supported:true,checkpoint_id:$('calibration-checkpoint').value});}});
$('confirm-recalibrate').addEventListener('click',()=>{if($('calibration-supported').checked){$('recalibrate-dialog').close();command('recalibrate-begin','RECALIBRATE',{supported:true});}});
$('stop').addEventListener('click',()=>command('stop'));
$('supported').addEventListener('change',()=>{$('confirm-release').disabled=!$('supported').checked;});
$('confirm-release').addEventListener('click',()=>{if($('supported').checked){$('release-dialog').close();command('release','RELEASE');}});
const stream=new EventSource(`/api/session?session=${encodeURIComponent(sessionId)}`);
stream.onopen=()=>{online=true;if(state)render(state);};
stream.onmessage=event=>{online=true;render(JSON.parse(event.data));};
stream.onerror=()=>{online=false;if(state)render(state);};
window.addEventListener('pagehide',()=>{closed=true;stream.close();navigator.sendBeacon(`/api/session/close?session=${encodeURIComponent(sessionId)}`);});
window.addEventListener('pageshow',event=>{if(event.persisted&&closed)location.reload();});

let calibrationStep=null;
const jointNames=['Shoulder pan','Shoulder lift','Elbow flex','Wrist flex','Wrist roll','Gripper'];
function renderGripper(next){
  const settings=next.gripper_force;
  const blocked=!online||busy||next.gripper_busy||next.calibration_busy||next.checking_connections||Boolean(next.recalibration)||['connecting','starting','resetting','releasing'].includes(next.phase);
  $('gripper-force').disabled=blocked;
  if(settings&&settings.read_at!==forceReadAt){
    forceReadAt=settings.read_at;
    $('force-slider').max=$('force-percent').max=settings.max_percent;
    $('force-slider').value=$('force-percent').value=settings.percent;
  }
  const percent=Number($('force-percent').value);
  const invalid=!Number.isInteger(percent)||percent<1||!settings||percent>settings.max_percent;
  for(const id of ['force-slider','force-percent'])$(id).disabled=blocked||!settings;
  $('apply-force').disabled=blocked||invalid;
  $('apply-force').textContent=next.gripper_busy?'Working…':'Apply force';
  $('gripper-current').textContent=next.gripper_busy?'Reading / applying motor 6 setting…':settings?`Current motor setting: ${settings.percent}% · Motor maximum: ${settings.max_percent}%`:'Motor setting unavailable. Close and reopen this panel to retry.';
  $('force-guidance').textContent=settings?`Protection setting: ${settings.overload_torque}; delay: ${settings.protection_time/100} s. No percentage is validated as overload-free; a low limit can still overload during a sustained grasp.`:'';
  const feedback=[settings,next.gripper_feedback].filter(Boolean).sort((a,b)=>b.read_at-a.read_at)[0];
  $('gripper-feedback').textContent=feedback?`Last sample: limit ${feedback.torque_limit/10}% · output ${((feedback.load&1023)/10).toFixed(1)}% · current ${(feedback.current*6.5).toFixed(0)} mA · ${feedback.temperature}°C · status ${feedback.status} · ${new Date(feedback.read_at*1000).toLocaleTimeString('en-GB',{})} local`:'';
  $('force-notice').textContent=next.gripper_error||clientError||next.error||(feedback?.status&32?'Motor 6 still reports overload. This control does not clear the fault or resume movement.':invalid&&settings?'Enter a whole percentage within the motor maximum.':'Apply force changes only motor 6 and verifies readback. Sustained gripping can still overload at a low setting.');
  $('force-stop').hidden=next.phase!=='following';
  $('force-stop').disabled=!online;
}
function renderLog(next){
  const warnings=Object.values(next.warnings||{});
  $('motion-warnings').hidden=!warnings.length;
  $('motion-warnings').textContent=`${warnings.length} active motion warning${warnings.length===1?'':'s'}. Warnings do not pause control. Details appear in the live log.`;
  const events=next.events||[],signature=JSON.stringify(events);
  if(signature===eventList)return;
  eventList=signature;
  const log=$('live-log'),atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-30;
  log.replaceChildren(...events.map(event=>{
    const row=document.createElement('div');row.className=`log-row ${event.level.toLowerCase()}`;
    const time=document.createElement('time');time.dateTime=event.time;time.textContent=event.time.slice(11,23);
    const level=document.createElement('strong');level.textContent=event.level;
    const message=document.createElement('span');message.textContent=event.message;
    row.append(time,level,message);return row;
  }));
  if(atBottom)log.scrollTop=log.scrollHeight;
}
function renderCheckpoints(next){
  const checkpoints=next.checkpoints||[],serialized=JSON.stringify(checkpoints),select=$('calibration-checkpoint');
  if(serialized!==checkpointList){
    const selected=select.value;checkpointList=serialized;
    select.replaceChildren(...checkpoints.map(checkpoint=>{
      const option=document.createElement('option');option.value=checkpoint.id;
      option.textContent=`${checkpoint.label}${checkpoint.current?' · Current':''}`;return option;
    }));
    select.value=checkpoints.some(c=>c.id===selected)?selected:(checkpoints.find(c=>c.current)||checkpoints[0])?.id||'';
  }
  const selected=checkpoints.find(c=>c.id===select.value);
  $('checkpoint-detail').textContent=selected?`${selected.current?'This is the current saved calibration. ':''}${selected.has_rest_pose?'Includes its saved resting pose.':'No matching resting pose; teach one before using Clear Error & Reset.'}`:'No saved checkpoints are available.';
  $('checkpoint-errors').hidden=!next.checkpoint_errors?.length;
  $('checkpoint-errors').textContent=next.checkpoint_errors?.join(' ')||'';
  const blocked=!online||busy||next.calibration_busy||next.checking_connections||Boolean(next.recalibration)||['connecting','starting','following','evaluating','resetting','releasing'].includes(next.phase);
  select.disabled=blocked||!checkpoints.length;
  $('confirm-recalibrate').disabled=blocked||!$('calibration-supported').checked;
  $('restore-calibration').disabled=blocked||!$('calibration-supported').checked||!selected||(selected.current&&next.calibration_ok);
}
function renderCalibration(next){
  const calibration=next.recalibration,step=calibration?.step;
  $('recalibration-panel').hidden=!calibration;
  if(calibrationStep!==step){calibrationStep=step;$('ranges-complete').checked=false;}
  if(!calibration)return;
  const titles={centers:'1. Capture joint centers',ranges:'2. Record movement ranges',review:'3. Review and save'};
  const instructions={centers:'Place both arms in the same physical pose, with each joint near the middle of its range and matching wrist/gripper orientation. Use the SO-101 guide below, then capture both centers.',ranges:'Move each joint on each arm through its full comfortable range, one at a time. The measured spans update below. When finished, review the ranges.',review:'Check that both arms were measured completely. Save writes the new centers and ranges to both arms, verifies them, and updates the calibration file. Both arms stay torque OFF.'};
  $('recalibration-title').textContent=titles[step];$('recalibration-instructions').textContent=instructions[step];
  $('range-table').hidden=step==='centers';$('range-confirmation').hidden=step!=='review';
  $('ranges').replaceChildren(...jointNames.map((name,index)=>{
    const row=document.createElement('tr'),label=document.createElement('td');label.textContent=name;row.append(label);
    for(const role of ['leader','follower']){const cell=document.createElement('td'),i=String(index+1),bounds=calibration.ranges?.[role]?.[i];cell.textContent=i==='5'?'Full encoder range':bounds?`${((bounds[1]-bounds[0])*360/4096).toFixed(1)}° (${bounds[0]}–${bounds[1]})`:'—';row.append(cell);}
    return row;
  }));
  $('calibration-next').textContent={centers:'Capture centers',ranges:'Review ranges',review:'Save new calibration'}[step];
  $('calibration-next').disabled=!online||busy||next.calibration_busy||(step==='review'&&!$('ranges-complete').checked);
  $('calibration-cancel').disabled=!online||busy||next.calibration_busy;
}
$('ranges-complete').addEventListener('change',()=>{if(state)renderCalibration(state);});
$('calibration-next').addEventListener('click',()=>{const step=state?.recalibration?.step;const actions={centers:['recalibrate-center','CENTER'],ranges:['recalibrate-review','REVIEW'],review:['recalibrate-save','SAVE_CALIBRATION']};if(actions[step])command(...actions[step]);});
$('calibration-cancel').addEventListener('click',()=>command('recalibrate-cancel','CANCEL'));

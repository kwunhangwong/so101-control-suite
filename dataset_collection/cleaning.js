const $=id=>document.getElementById(id);
let episode=null,current=0,excluded=new Set(),splitExcluded=new Set(),loading=false,saving=false,loadVersion=0,operationLocked=false,mode='seed',cleanup={},bufferDelete=null;
let cleanedBuffer='',autoExcluded=new Set(),bufferRecords=[],bufferMerge=null;
const localBuffer=()=>mode==='bootstrap';
const mode04Buffer=()=>mode==='bootstrap'&&$('clean-buffer').value.startsWith('mode04:');
const drafts=new Map();
const draftKey=id=>`${mode}:${mode==='cleaned'?cleanedBuffer:$('clean-buffer').value}:${id}`;
function error(message=''){$('clean-error').textContent=message;$('clean-error').hidden=!message;}
function endpoint(path){
  const prefix=mode==='bootstrap'?'/api/bootstrap-cleaning/':mode==='cleaned'?'/api/cleaned-buffer/':'/api/cleaning/';
  const buffer=mode==='cleaned'?cleanedBuffer:$('clean-buffer').value;
  return prefix+path+(mode!=='seed'?(path.includes('?')?'&':'?')+'buffer='+encodeURIComponent(buffer):'');
}
async function api(path,body){
  const response=await fetch(endpoint(path),body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,...(mode==='bootstrap'?{buffer:$('clean-buffer').value}:mode==='cleaned'?{buffer:cleanedBuffer}:{} )})}:{cache:'no-store'});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not load this episode.');return data;
}
export function updateCleaningAvailability(state,busy){
  const physical=busy||state.bootstrap?.active||state.bootstrap?.checking||state.checking_connections||state.gripper_busy||['recording','awaiting_label'].includes(state.bootstrap?.episode?.status)||state.bootstrap?.episode?.robot_finished===false||['starting','following','evaluating','bootstrapping','resetting','calibrating','releasing'].includes(state.phase)||['recording','awaiting_label'].includes(state.episode?.status)||state.episode?.robot_finished===false||['recording','awaiting_label'].includes(state.evaluation?.episode?.status)||state.evaluation?.episode?.robot_finished===false;
  cleanup=state.bootstrap_cleaning||{};operationLocked=physical||cleanup.busy||cleanup.pending;
  $('clean-view-button').disabled=physical||cleanup.busy||cleanup.pending||saving;
  $('bootstrap-clean-view-button').disabled=true;
  $('record-view-button').disabled=saving;
  $('clean-view-button').title=physical?'End and label the episode, and wait for movement to finish.':'';
  $('clean-sync-status').hidden=mode!=='bootstrap'||!cleanup.message;
  $('clean-sync-status').textContent=cleanup.message||'';
  $('clean-retry').hidden=mode!=='bootstrap'||!cleanup.pending||cleanup.busy;
  $('clean-retry').disabled=physical||saving||cleanup.busy;
  if(mode==='bootstrap'&&cleanup.error&&!saving)error(cleanup.error);
  updateControls();
}
function switchView(next){
  if(saving||loading)return;
  clearPreview();
  mode=next==='bootstrap'?'bootstrap':next==='cleaned'?'cleaned':'seed';loadVersion++;episode=null;cancelBufferDelete();cancelBufferMerge();
  $('record-view').hidden=next!=='record';$('clean-view').hidden=next==='record';
  for(const [id,value] of [['record-view-button','record'],['clean-view-button','seed'],['bootstrap-clean-view-button','bootstrap']]){
    $(id).classList.toggle('primary',(next==='cleaned'?'seed':next)===value);$(id).setAttribute('aria-pressed',String((next==='cleaned'?'seed':next)===value));
  }
  const boot=mode==='bootstrap',cleaned=mode==='cleaned';
  $('clean-heading').textContent=boot?'Bootstrap buffer cleaning':cleaned?'Seed data cleaning · cleaned buffer':'Seed data cleaning';
  $('clean-preview').hidden=false;$('cleaned-buffer-picker').hidden=boot;$('clean-delete').hidden=cleaned;
  $('clean-source-mode').textContent=cleaned?'Cleaned buffer: confirmed changes permanently replace the selected episode in this folder. To recover removed frames, create a new cleaned copy from the original recording.':'Original recordings: Save cleaned copy keeps the source recording.';
  $('clean-noops-controls').hidden=false;$('clean-noops').hidden=false;$('clean-split').hidden=false;$('clean-noops-note').hidden=false;
  $('clean-buffer-picker').hidden=!boot;$('clean-token-usage').hidden=!boot;
  $('clean-delete-note').textContent=boot?'Permanently removes this local rollout and its local derived copies. Original seeds and other episodes are kept.':'Permanently removes this recording, including both camera views, joint data, and metadata. Cleaned copies are kept.';
  $('clean-delete-yes').textContent=boot?'Delete local episode':'Delete permanently';
  $('clean-help').textContent=boot?'No-op removal and ordinary exclusions join retained frames. Remove section & split keeps separate sequences inside this same episode folder. Existing split boundaries are preserved. Changes are local and permanent after confirmation.':'Frame numbers start at 1. Excluding a frame removes both camera views and its time interval from the cleaned copy. Ordinary exclusions join the retained frames into one trajectory. Remove section & split saves separate trajectories on either side. Source recordings stay available.';
  $('clean-save').textContent=boot||cleaned?'Save changes in place':'Save cleaned copy';
  $('clean-split').textContent=cleaned||boot?'Remove section & split sequence':'Remove section & split';
  if(cleaned)$('clean-help').textContent='Frame numbers start at 1. Exclusions remove both views and the aligned robot time intervals. Ordinary exclusions join retained frames. Remove section & split sequence marks separate segments inside this same episode folder. Restore only reverses unsaved selections.';
  $('clean-output-label').textContent=boot?'Updated local episode directory':cleaned?'Updated cleaned episode directory':'Latest cleaned copy directory';
  $('clean-editor').hidden=true;$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;$('clean-delete-status').textContent='';
  if(next==='bootstrap')refreshBuffers();else if(next==='seed'||next==='cleaned')refresh();
}
$('record-view-button').addEventListener('click',()=>switchView('record'));
$('clean-view-button').addEventListener('click',()=>switchView('seed'));
$('bootstrap-clean-view-button').addEventListener('click',()=>switchView('bootstrap'));
$('cleaned-buffer-load').addEventListener('click',()=>{
  if(loading||saving||operationLocked)return;
  cleanedBuffer=$('cleaned-buffer-path').value.trim();
  switchView('cleaned');
});
$('clean-original-load').addEventListener('click',()=>switchView('seed'));
async function refreshBuffers(){
  if(loading||saving)return;
  cancelBufferDelete();
  cancelBufferMerge();
  loading=true;error();updateControls();
  try{
    const data=await api('buffers'),selected=$('clean-buffer').value;
    bufferRecords=data.buffers;
    $('clean-buffer').replaceChildren(new Option('Choose a buffer…',''),...data.buffers.map(b=>new Option(b.run_name?`Mode 04 · ${b.run_name} · ${b.stage_label} · ${b.episodes} episodes`:`${b.task_name} · ${new Date(b.started_at).toLocaleString()} · ${b.episodes} episodes`,b.id)));
    if(data.buffers.some(b=>b.id===selected))$('clean-buffer').value=selected;
    episode=null;$('clean-editor').hidden=true;$('clean-task').textContent='Choose a buffer to list its recorded rollouts.';$('clean-source').textContent='';$('clean-token-usage').textContent='';
    $('clean-episode').replaceChildren(new Option('Choose a buffer first',''));
  }catch(e){error(e.message);}finally{loading=false;updateControls();}
  if($('clean-buffer').value)await refresh();
}
$('clean-buffer-refresh').addEventListener('click',refreshBuffers);
$('clean-buffer').addEventListener('change',()=>{cancelBufferDelete();cancelBufferMerge();episode=null;refresh();});
function bufferLabels(){
  if(mode!=='bootstrap')return;
  const mode04=mode04Buffer();
  $('clean-buffer-note').textContent=(mode04?'Mode 04 run/stage buffer. ':'')+'Confirmed edits replace recordings locally in place. Preprocess and upload to the GPU host separately when needed.';
  $('clean-delete-note').textContent=mode04?'Permanently removes this local rollout, both camera views, robot data and logs. Other episodes and stages are kept.':'Permanently removes this local rollout and its local derived copies. Original seeds and other episodes are kept.';
  $('clean-delete-yes').textContent='Delete local episode';
  $('clean-help').textContent='No-op removal and ordinary exclusions join retained frames. Remove section & split keeps separate sequences inside this same episode folder. Existing split boundaries are preserved. Changes are local and permanent after confirmation.';
  $('clean-output-label').textContent='Updated local episode directory';
  $('clean-token-usage').hidden=mode04;
  $('clean-buffer-delete').textContent=mode04?'Delete Stage Buffer':'Delete Buffer';
}
async function refresh(){
  bufferLabels();
  if(loading||saving)return;
  if(mode==='bootstrap'&&!$('clean-buffer').value){episode=null;$('clean-editor').hidden=true;$('clean-episode').replaceChildren(new Option('Choose a buffer first',''));updateControls();return;}
  loading=true;error();$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;updateControls();
  try{
    const data=await api('episodes'),select=$('clean-episode'),selected=select.value;
    if(mode==='cleaned'){cleanedBuffer=data.buffer;$('cleaned-buffer-path').value=data.buffer;}
    select.replaceChildren(...data.episodes.map(e=>new Option(`${mode==='cleaned'?e.id+' · ':localBuffer()?'':e.task_name?e.task_name+' · ':''}${new Date(e.started_at).toLocaleString()} · ${e.label} · ${e.frames} frames`,e.id)));
    if(!data.episodes.length){select.add(new Option('No finished episodes yet',''));episode=null;$('clean-editor').hidden=true;$('clean-task').textContent=mode==='cleaned'?'No cleaned episodes found in this folder. Choose a folder containing cleaned episode directories.':'End and label a recording to review it here.';$('clean-source').textContent='';$('clean-token-usage').textContent='';}
    else {if(data.episodes.some(e=>e.id===selected))select.value=selected;await loadEpisode(select.value);}
  }catch(e){error(e.message);}finally{loading=false;updateControls();}
}
async function loadEpisode(id){
  clearPreview();
  const version=++loadVersion;episode=null;
  $('clean-editor').hidden=true;$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;updateControls();error();
  try{
    const data=await api('episode?episode='+encodeURIComponent(id));
    if(version!==loadVersion)return;
    episode=data;current=0;let draft=drafts.get(draftKey(id));
    if(mode!=='seed'&&draft?.revision!==data.revision){drafts.delete(draftKey(id));draft=null;}
    excluded=new Set(draft?.excluded||[]);splitExcluded=new Set(draft?.split||[]);autoExcluded=new Set((draft?.autoExcluded||[]).filter(i=>excluded.has(i)));
    $('clean-task').textContent=`${data.metadata.context?.instruction||'No task instruction'} — label: ${data.metadata.label}`;
    $('clean-source').textContent=data.path;
    const usage=data.token_usage||{};
    $('clean-token-usage').textContent=`${(usage.total_tokens||0).toLocaleString()} reported tokens for this episode${usage.unreported_calls?` · ${usage.unreported_calls} calls missing usage`:''}. Clipping keeps this usage; deleting removes it from retained-buffer totals.`;
    $('clean-scrubber').max=data.frames.length-1;
    for(const id of ['clean-from','clean-to']){$(id).max=data.frames.length;$(id).value=1;}
    $('clean-noops-status').textContent='';$('clean-save-status').textContent='';$('clean-output').textContent=mode!=='seed'?'No changes saved in this session.':'No cleaned copy saved in this session.';
    $('clean-editor').hidden=false;renderSelection();showFrame();
  }catch(e){if(version===loadVersion){episode=null;error(e.message);}}finally{updateControls();}
}
function updateControls(){
  const locked=operationLocked||loading||saving,disabled=locked||!episode||episode.id!==$('clean-episode').value;
  $('clean-delete').disabled=disabled||mode==='cleaned';$('clean-delete-yes').disabled=disabled||mode==='cleaned';
  $('clean-preview').disabled=disabled||excluded.size===episode?.frames.length;
  $('clean-save').disabled=disabled||excluded.size===episode?.frames.length||(mode!=='seed'&&!excluded.size);
  $('clean-save-yes').disabled=disabled||!excluded.size||excluded.size===episode?.frames.length||(mode==='bootstrap'&&!$('clean-outcome').value);
  for(const id of ['cleaned-buffer-path','cleaned-buffer-load','clean-original-load'])$(id).disabled=locked;
  $('clean-buffer').disabled=locked;$('clean-buffer-refresh').disabled=locked;
  $('clean-buffer-delete').disabled=locked||mode!=='bootstrap'||!$('clean-buffer').value;
  $('clean-buffer-delete-yes').disabled=locked||!bufferDelete||bufferDelete.id!==$('clean-buffer').value;
  $('clean-buffer-merge').disabled=locked||mode!=='bootstrap'||!$('clean-buffer').value||mode04Buffer();
  $('clean-buffer-merge-source').disabled=locked;
  $('clean-buffer-merge-yes').disabled=locked||!bufferMerge||bufferMerge.target.id!==$('clean-buffer').value||bufferMerge.source.id!==$('clean-buffer-merge-source').value;
  $('clean-episode').disabled=locked||(mode==='bootstrap'&&!$('clean-buffer').value);
  $('clean-refresh').disabled=locked||(mode==='bootstrap'&&!$('clean-buffer').value);
  for(const id of ['clean-noops','clean-noops-threshold','clean-split','clean-toggle','clean-from','clean-to','clean-mark-from','clean-mark-to','clean-exclude','clean-restore','clean-restore-all','clean-outcome'])$(id).disabled=disabled;
}
$('clean-delete').addEventListener('click',()=>{if(!episode||$('clean-delete').disabled)return;$('clean-delete-path').textContent=episode.path;$('clean-delete-confirm').hidden=false;});
$('clean-delete-cancel').addEventListener('click',()=>{$('clean-delete-confirm').hidden=true;});
function cancelBufferDelete(){bufferDelete=null;$('clean-buffer-delete-confirm').hidden=true;}
$('clean-buffer-delete').addEventListener('click',async()=>{
  if($('clean-buffer-delete').disabled)return;
  cancelBufferMerge();
  loading=true;error();updateControls();
  try{
    bufferDelete=await api('buffer');
    $('clean-buffer-delete-summary').textContent=`${bufferDelete.id} · ${bufferDelete.episodes.length} recorded episodes`;
    $('clean-buffer-delete-path').textContent=bufferDelete.path;
    $('clean-buffer-delete-heading').textContent=mode04Buffer()?'Delete this local stage buffer?':'Delete this entire local buffer?';
    $('clean-buffer-delete-yes').textContent=mode04Buffer()?'Delete local stage buffer':'Delete local buffer';
    $('clean-buffer-delete-note').textContent=mode04Buffer()?'Permanently removes every episode and file in this selected local stage folder. Other stages, runs and original seeds are kept. This cannot be undone.':'Permanently removes every rollout in this buffer, their images, robot data, logs and cached seed/reference copies. The original cleaned seed and other buffers stay available. This cannot be undone.';
    $('clean-buffer-delete-confirm').hidden=false;
    $('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;
  }catch(e){cancelBufferDelete();error(e.message);}finally{loading=false;updateControls();}
});
$('clean-buffer-delete-cancel').addEventListener('click',()=>{cancelBufferDelete();updateControls();});
$('clean-buffer-delete-yes').addEventListener('click',()=>{
  if($('clean-buffer-delete-yes').disabled)return;
  mutate('delete-buffer',{revision:bufferDelete.revision,confirm:'DELETE_BOOTSTRAP_BUFFER'});
});
function cancelBufferMerge(){bufferMerge=null;$('clean-buffer-merge-confirm').hidden=true;}
$('clean-buffer-merge').addEventListener('click',()=>{
  if($('clean-buffer-merge').disabled)return;
  cancelBufferDelete();bufferMerge=null;
  const target=bufferRecords.find(b=>b.id===$('clean-buffer').value);
  const candidates=bufferRecords.filter(b=>!b.run_name&&b.id!==target.id&&b.task_id===target.task_id);
  $('clean-buffer-merge-source').replaceChildren(new Option('Choose another buffer…',''),...candidates.map(b=>new Option(`${b.id} · ${b.episodes} episodes`,b.id)));
  $('clean-buffer-merge-summary').textContent=candidates.length?`Destination: ${target.id} · ${target.episodes} episodes`:'No other buffers for this task are available.';
  $('clean-buffer-merge-confirm').hidden=false;$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;updateControls();
});
$('clean-buffer-merge-source').addEventListener('change',async()=>{
  bufferMerge=null;error();
  const source=$('clean-buffer-merge-source').value;
  if(!source){$('clean-buffer-merge-summary').textContent='Choose another buffer to review the merge.';updateControls();return;}
  loading=true;updateControls();
  try{
    bufferMerge=await api('merge?source='+encodeURIComponent(source));
    const {target,source:from,seed_count}=bufferMerge;
    $('clean-buffer-merge-summary').textContent=`Keep ${target.id}: ${target.episodes.length} + ${from.episodes.length} = ${target.episodes.length+from.episodes.length} recorded episodes; ${seed_count} original seed references. Remove source folder: ${from.path}`;
  }catch(e){$('clean-buffer-merge-summary').textContent='Merge unavailable.';error(e.message);}finally{loading=false;updateControls();}
});
$('clean-buffer-merge-cancel').addEventListener('click',()=>{cancelBufferMerge();updateControls();});
$('clean-buffer-merge-yes').addEventListener('click',()=>{
  if($('clean-buffer-merge-yes').disabled)return;
  mutate('merge',{source:bufferMerge.source.id,revision:bufferMerge.revision,confirm:'MERGE_BOOTSTRAP_BUFFERS'});
});
async function waitOperation(){
  for(;;){
    const state=await api('status');$('clean-sync-status').hidden=false;$('clean-sync-status').textContent=state.message||'';
    if(!state.busy){if(state.status!=='complete')throw new Error(state.error||state.message);return state.result;}
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
}
async function mutate(action,body){
  if(saving)return;
  saving=true;error();const id=episode?.id;
  $('clean-save-status').textContent='Saving locally…';
  const controls=[...document.querySelectorAll('#clean-view button,#clean-view input,#clean-view select')];controls.forEach(control=>control.disabled=true);
  try{
    let result=await api(action,body);
    if(mode==='bootstrap')result=await waitOperation();
    clearPreview();
    drafts.delete(draftKey(id));
    if(result.action==='merge'){
      for(const key of drafts.keys())if([result.buffer_id,result.source_id].some(b=>key.startsWith(`bootstrap:${b}:`)))drafts.delete(key);
      cancelBufferMerge();episode=null;$('clean-editor').hidden=true;
      saving=false;await refreshBuffers();
      $('clean-delete-status').textContent=`Merged ${result.episodes_merged} episodes into ${result.buffer_id}: ${result.episodes_total} episodes, ${result.seed_count} original seeds. Prepare local buffer before starting GPT again.`;
      return;
    }
    if(result.action==='delete-buffer'){
      for(const key of drafts.keys())if(key.startsWith(`bootstrap:${result.buffer_id}:`))drafts.delete(key);
      cancelBufferDelete();episode=null;$('clean-editor').hidden=true;$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;
      saving=false;await refreshBuffers();
      $('clean-delete-status').textContent=`Deleted local buffer and ${result.episodes_deleted} recorded episodes. Other buffers and original seeds kept.`;
      return;
    }
    if(mode!=='seed'||action==='delete'){
      episode=null;$('clean-editor').hidden=true;$('clean-delete-confirm').hidden=true;$('clean-save-confirm').hidden=true;saving=false;await refresh();
    }
    $('clean-output').textContent=result.paths?.join('\n')||result.path||'';
    $('clean-save-status').textContent=mode==='cleaned'?`Saved in place: ${result.frames_before} → ${result.frames_after} frame pairs. Removed frames are permanently deleted from this cleaned episode.`:result.local_only?`Saved locally in place: ${result.frames_before} → ${result.frames_after} frame pairs.`:action==='delete'?`Deleted ${id}. Cleaned copies were kept.`:`Saved ${result.frames} frame pairs as ${result.segments} separate trajectory folder(s). Ordinary frame exclusions are joined within each trajectory. Original episode preserved.`;
    if(action==='delete')$('clean-delete-status').textContent=result.local_only?`Deleted ${id} from this Mac.`:`Deleted ${id}. Cleaned copies were kept.`;
  }catch(e){error(e.message);$('clean-save-status').textContent='Save failed. Your selection is still here. Retry local cleanup if shown.';}
  finally{saving=false;controls.forEach(control=>control.disabled=false);renderSelection();showFrame();updateControls();}
}
$('clean-delete-yes').addEventListener('click',()=>{if(!episode||$('clean-delete-yes').disabled)return;mutate('delete',{episode:episode.id,revision:episode.revision,confirm:mode==='bootstrap'?'DELETE_BOOTSTRAP_EPISODE':'DELETE_EPISODE'});});
$('clean-retry').addEventListener('click',()=>mutate('retry',{}));
$('clean-refresh').addEventListener('click',refresh);
$('clean-episode').addEventListener('change',()=>loadEpisode($('clean-episode').value));
function showFrame(){
  if(!episode)return;
  current=Math.max(0,Math.min(episode.frames.length-1,current));$('clean-scrubber').value=current;
  const f=episode.frames[current],removed=excluded.has(current);
  $('clean-frame-info').textContent=`Frame ${current+1} / ${episode.frames.length} · ${f.elapsed_seconds.toFixed(2)} s · ${splitExcluded.has(current)?'REMOVED SECTION · SPLIT':removed?'EXCLUDED':'KEPT'}${f.has_robot?'':' · No paired joint sample'}`;
  $('clean-frame-info').classList.toggle('excluded',removed);$('clean-toggle').textContent=removed?'Restore this frame':'Exclude this frame';
  $('clean-prev').disabled=current===0;$('clean-next').disabled=current===episode.frames.length-1;
  const version=loadVersion,shown=current;let loaded=0;
  for(const name of ['front','wrist']){
    const img=$('clean-'+name);img.hidden=true;
    img.onload=()=>{if(version===loadVersion&&shown===current&&++loaded===2){$('clean-front').hidden=false;$('clean-wrist').hidden=false;}};
    img.onerror=()=>error('A recorded camera image could not be loaded. Try another frame.');
    img.src=endpoint(`frame?episode=${encodeURIComponent(episode.id)}&index=${current}&camera=${name}&revision=${episode.revision||''}`);
  }
}
function renderSelection(){
  if(!episode)return;const total=episode.frames.length,ranges=[];
  for(const i of [...excluded].sort((a,b)=>a-b)){if(!ranges.length||i>ranges.at(-1)[1]+1)ranges.push([i,i]);else ranges.at(-1)[1]=i;}
  let parts=0,inside=false,lastSegment=null;for(let i=0;i<total;i++){const segment=episode.frames[i].segment_id??0;if(mode==='bootstrap'&&segment!==lastSegment)inside=false;lastSegment=segment;if(splitExcluded.has(i))inside=false;else if(!excluded.has(i)&&!inside){parts++;inside=true;}}
  $('clean-summary').textContent=`${total-excluded.size} kept · ${excluded.size} excluded · ${total} original frame pairs${mode==='seed'?` · ${parts} trajectory folder(s) on save`:mode==='bootstrap'?` · ${parts} sequence(s) · saves in this same folder`:mode==='cleaned'?' · saves in this same folder':''}`;
  $('clean-ranges').textContent=ranges.length?'Excluded ranges: '+ranges.map(([a,b])=>a===b?`${a+1}`:`${a+1}–${b+1}`).join(', '):'No frames excluded.';
  $('clean-timeline').replaceChildren(...episode.frames.map((_,i)=>{const span=document.createElement('span');span.className=excluded.has(i)?'excluded':'';span.title=splitExcluded.has(i)?`Frame ${i+1}: removed section / split`:`Frame ${i+1}`;if(splitExcluded.has(i))span.style.background='#d69e36';return span;}));updateControls();
}
function changed(){clearPreview();drafts.set(draftKey(episode.id),{excluded:[...excluded],split:[...splitExcluded],autoExcluded:[...autoExcluded],revision:episode.revision});$('clean-save-confirm').hidden=true;$('clean-save-status').textContent='Unsaved selection';renderSelection();showFrame();}
$('clean-scrubber').addEventListener('input',()=>{current=Number($('clean-scrubber').value);showFrame();});
$('clean-prev').addEventListener('click',()=>{current--;showFrame();});$('clean-next').addEventListener('click',()=>{current++;showFrame();});
$('clean-toggle').addEventListener('click',()=>{excluded.has(current)?excluded.delete(current):excluded.add(current);splitExcluded.delete(current);autoExcluded.delete(current);changed();});
$('clean-mark-from').addEventListener('click',()=>{$('clean-from').value=current+1;});$('clean-mark-to').addEventListener('click',()=>{$('clean-to').value=current+1;});
function range(remove,split=false){
  const from=Number($('clean-from').value)-1,to=Number($('clean-to').value)-1;
  if(!Number.isInteger(from)||!Number.isInteger(to)||from<0||to<from||to>=episode.frames.length){error('Choose a valid range: From must be no later than To.');return;}
  error();for(let i=from;i<=to;i++){remove?excluded.add(i):excluded.delete(i);autoExcluded.delete(i);if(split&&remove)splitExcluded.add(i);else splitExcluded.delete(i);}changed();
}
$('clean-exclude').addEventListener('click',()=>range(true));$('clean-restore').addEventListener('click',()=>range(false));$('clean-restore-all').addEventListener('click',()=>{excluded.clear();splitExcluded.clear();autoExcluded.clear();$('clean-noops-status').textContent='';changed();});
$('clean-split').addEventListener('click',()=>range(true,true));
$('clean-noops').addEventListener('click',async()=>{
  if(!episode||$('clean-noops').disabled)return;
  const raw=$('clean-noops-threshold').value.trim(),tolerance=Number(raw);
  if(!raw||!Number.isInteger(tolerance)||tolerance<0||tolerance>4095){error('Enter a whole pose threshold from 0 to 4095 encoder ticks.');return;}
  loading=true;error();updateControls();
  $('clean-noops-status').textContent='Comparing consecutive poses; preserving gripper changes…';
  try{
    const manual=[...excluded].filter(i=>!autoExcluded.has(i)).sort((a,b)=>a-b);
    const data=await api('no-ops?episode='+encodeURIComponent(episode.id)+'&tolerance_ticks='+tolerance+'&excluded='+encodeURIComponent(JSON.stringify(manual)));
    if(mode!=='seed'&&data.revision!==episode.revision)throw new Error('This episode changed. Reload it before editing.');
    excluded=new Set(manual);autoExcluded=new Set(data.excluded);for(const i of autoExcluded)excluded.add(i);
    changed();$('clean-noops-status').textContent=`${autoExcluded.size} stationary frame(s) marked at ${tolerance} ticks; gripper changes preserved. Manual exclusions kept.${data.unscored_indices?.length?` ${data.unscored_indices.length} frame(s) kept because joint data is missing or invalid.`:''} Review the red frames before saving; Restore reverses exclusions.`;
  }catch(e){error(e.message);$('clean-noops-status').textContent='No-op detection failed; selection kept.';}
  finally{loading=false;updateControls();}
});
function clearPreview(){
  const video=$('clean-preview-video');video.pause();video.removeAttribute('src');video.load();video.hidden=true;
  $('clean-preview-panel').hidden=true;$('clean-preview-status').textContent='';$('clean-preview-source').textContent='';
}
async function generatePreview(){
  if(!episode||$('clean-preview').disabled)return;
  loading=true;clearPreview();error();updateControls();
  $('clean-preview-panel').hidden=false;$('clean-preview-source').textContent=episode.path;
  $('clean-preview-status').textContent='Generating clipped MP4 from the current episode and selection…';
  try{
    const data=await api('preview',{episode:episode.id,revision:episode.revision,excluded:[...excluded].sort((a,b)=>a-b),split_excluded:[...splitExcluded].sort((a,b)=>a-b)});
    // The file is replaced in place; a fresh URL prevents reuse of cached video bytes.
    const video=$('clean-preview-video');video.src=data.url+'?v='+crypto.randomUUID();video.hidden=false;video.load();
    $('clean-preview-status').textContent=`${data.frames} retained frame pairs · ${data.duration_seconds.toFixed(2)} s · ${data.segments} retained sequence(s)${data.playback_hz?` · ${data.playback_hz} Hz playback; GPT thinking pauses omitted`:''}. Preview only; episode data has not been saved.`;
  }catch(e){error(e.message);$('clean-preview-status').textContent='Preview generation failed. Your selection is still here.';}
  finally{loading=false;updateControls();}
}
$('clean-preview').addEventListener('click',generatePreview);
$('clean-preview-video').addEventListener('error',()=>{
  if($('clean-preview-video').getAttribute('src'))$('clean-preview-status').textContent='This temporary preview is unavailable. Generate it again.';
});
$('clean-save').addEventListener('click',()=>{
  if(saving||!episode||$('clean-save').disabled)return;
  if(mode!=='seed'){
    const cleaned=mode==='cleaned';
    $('clean-save-heading').textContent=cleaned?'Permanently remove the selected frames?':'Replace this local episode in place?';
    $('clean-save-note').textContent=cleaned?`${excluded.size} of ${episode.frames.length} camera pairs and their aligned robot time intervals will be removed from this cleaned episode. This cannot be undone here. Recovery requires creating a new cleaned copy from the original seed recording.`:'The excluded camera pairs and aligned robot samples will be removed permanently from this local episode. Ordinary exclusions join retained frames; explicit cuts remain separate sequences in this folder. Confirm the outcome of the retained clip.';
    $('clean-save-path').textContent=episode.path;$('clean-outcome-field').hidden=cleaned;
    $('clean-save-yes').textContent=cleaned?'Confirm permanent removal':'Replace local episode';
    $('clean-outcome').value=!cleaned&&['success','failure'].includes(episode.metadata.label)?episode.metadata.label:'';$('clean-save-confirm').hidden=false;updateControls();
  }
  else mutate('save',{episode:episode.id,excluded:[...excluded].sort((a,b)=>a-b),split_excluded:[...splitExcluded].sort((a,b)=>a-b)});
});
$('clean-outcome').addEventListener('change',updateControls);
$('clean-save-cancel').addEventListener('click',()=>{$('clean-save-confirm').hidden=true;});
$('clean-save-yes').addEventListener('click',()=>{if(!episode||$('clean-save-yes').disabled)return;mutate('save',{episode:episode.id,revision:episode.revision,excluded:[...excluded].sort((a,b)=>a-b),split_excluded:[...splitExcluded].sort((a,b)=>a-b),label:$('clean-outcome').value,confirm:mode==='cleaned'?'REPLACE_CLEANED_EPISODE':'REPLACE_BOOTSTRAP_EPISODE'});});

const $ = id => document.getElementById(id);
const frame=$('frame'), crop=$('crop'), ctx=frame.getContext('2d'), preview=crop.getContext('2d');
const fields=['left','top','right','bottom'].map($);
const cameras=Object.fromEntries(['front','wrist'].map(name=>[name,{
  name, title:name==='front'?'Front camera':'Wrist camera', image:new Image(), liveImage:null,
  saved:null, bounds:null, inputs:[], width:0, height:0, ready:false, dirty:false,
  validInputs:true, saving:false, timer:null, token:0, connection:'reference', error:null,
  controls:null, controlsDraft:null, controlsBusy:false, controlsMessage:'Reading hardware settings…', controlsError:false,
  source:{source_kind:'snapshot',camera_label:`${name} camera`,camera_browser_device_id:null}
}]));
let active=cameras.front, drag=null;
const sessionId=crypto.randomUUID();
let session=null,closing=false;
const cameraRequest=path=>fetch(`${path}?session=${encodeURIComponent(sessionId)}`,{method:'POST'});
function openSession(){
  return new Promise((resolve,reject)=>{
    session=new EventSource(`/api/session?session=${encodeURIComponent(sessionId)}`);
    session.onopen=resolve;
    session.onerror=()=>{session.close();reject(new Error('Could not keep the camera connection open. Reload the app to reconnect.'));};
  });
}
const clamp=(v,min,max)=>Math.max(min,Math.min(max,v));
const valid=(b,c=active)=>b.length===4 && b.every(Number.isInteger) && b[0]>=0 && b[1]>=0 && b[0]<b[2] && b[1]<b[3] && b[2]<=c.width && b[3]<=c.height;
const scaleBounds=(b,from,to)=>{
  const p=b.map((v,i)=>Math.round(v*to[i%2]/from[i%2]));
  return [clamp(p[0],0,to[0]-1),clamp(p[1],0,to[1]-1),clamp(p[2],p[0]+1,to[0]),clamp(p[3],p[1]+1,to[1])];
};
function status(text,error=false){$('status').textContent=text;$('status').style.color=error?'#f1a79b':'';}
function hasChanges(c){return !c.saved || c.bounds.some((v,i)=>v!==c.saved.bounds_xyxy[i]) || c.width!==c.saved.source_size_wh[0] || c.height!==c.saved.source_size_wh[1] || c.source.source_kind!==c.saved.source_kind || (c.source.source_kind==='live' && c.source.camera_driver!==c.saved.camera_driver);}
function updateActions(){
  $('save').disabled=!active.ready || !active.dirty || !active.validInputs || active.saving || active.connection==='starting';
  $('revert').disabled=!active.ready || (!active.dirty && active.validInputs) || active.saving;
  $('save-state').textContent=active.saving?'Saving…':active.dirty?'Unsaved changes':'Saved';
  $('save-state').classList.toggle('dirty',active.dirty);
}
function syncControls(){
  $('frame-heading').textContent=active.title;
  $('config-path').textContent=`config/${active.name}_roi.json`;
  const live=active.connection==='live';
  $('source-badge').textContent=live?'LIVE CAMERA':active.connection==='starting'?'CONNECTING':'REFERENCE IMAGE';
  $('source-badge').classList.toggle('live',live);
  $('source-label').textContent=live?`${active.title} · ${active.cameraId}`:`Saved ${active.name}-camera reference`;
  $('connect-camera').textContent=`Reconnect ${active.name} camera`;
  $('connect-camera').disabled=!active.ready || active.connection==='starting';
  $('connect-camera').hidden=active.connection!=='reference';
  renderCameraControls();
  fields.forEach((input,i)=>{input.value=active.inputs[i]??'';input.max=i%2===0?active.width-(i<2?1:0):active.height-(i<2?1:0);});
  $('bounds-error').textContent=active.validInputs?'':'Enter integer bounds inside the source frame, with right > left and bottom > top.';
  $('source-size').textContent=`${active.width} × ${active.height} px`;
  frame.setAttribute('aria-label',`${active.title} ROI editor. Drag inside to move, drag corners to resize, or drag outside to draw. Arrow keys move one pixel; Shift+arrows move ten.`);
  for(const c of Object.values(cameras)) $('select-'+c.name).setAttribute('aria-pressed',String(c===active));
  updateActions();draw();
}
function setBounds(next,changed=true){
  active.bounds=next;active.inputs=next.map(String);active.validInputs=true;
  if(changed) active.dirty=hasChanges(active);
  syncControls();
}
function draw(){
  if(!active.ready) return;
  const {width,height,bounds}=active,media=active.liveImage||active.image;
  if(frame.width!==width || frame.height!==height){frame.width=width;frame.height=height;}
  ctx.drawImage(media,0,0,width,height);
  const [l,t,r,b]=bounds,w=r-l,h=b-t;
  ctx.fillStyle='rgba(6,13,23,0.62)';
  ctx.fillRect(0,0,width,t);ctx.fillRect(0,b,width,height-b);ctx.fillRect(0,t,l,h);ctx.fillRect(r,t,width-r,h);
  const unit=width/Math.max(1,frame.clientWidth);
  ctx.strokeStyle='#72d9ed';ctx.lineWidth=2*unit;ctx.strokeRect(l,t,w,h);ctx.fillStyle='#baf3ff';
  for(const [x,y] of [[l,t],[r,t],[l,b],[r,b]])ctx.fillRect(x-4*unit,y-4*unit,8*unit,8*unit);
  if(crop.width!==w || crop.height!==h){crop.width=w;crop.height=h;}
  preview.drawImage(media,l,t,w,h,0,0,w,h);
  $('crop-size').textContent=`${w} × ${h}`;
  frame.dataset.camera=active.name;frame.dataset.sequence=active.sequence||0;
}
function drawTile(c){
  if(!c.ready)return;
  const canvas=$('preview-'+c.name),context=canvas.getContext('2d'),media=c.liveImage||c.image;
  context.clearRect(0,0,canvas.width,canvas.height);
  const ratio=Math.min(canvas.width/c.width,canvas.height/c.height),w=c.width*ratio,h=c.height*ratio;
  context.drawImage(media,(canvas.width-w)/2,(canvas.height-h)/2,w,h);
  canvas.dataset.sequence=c.sequence||0;
  const tag=$('state-'+c.name);
  tag.textContent=c.connection==='live'?'Live':c.connection==='starting'?'Connecting':c.error?'Unavailable':'Reference';
  tag.classList.toggle('live',c.connection==='live');
}
function selectCamera(c){
  active=c;drag=null;syncControls();
  status(c.error||`${c.title} selected. Changes save to config/${c.name}_roi.json.`,Boolean(c.error));
}
for(const c of Object.values(cameras))$('select-'+c.name).addEventListener('click',()=>selectCamera(c));
function pointer(event){const rect=frame.getBoundingClientRect();return [clamp(Math.round((event.clientX-rect.left)*active.width/rect.width),0,active.width),clamp(Math.round((event.clientY-rect.top)*active.height/rect.height),0,active.height)];}
function hit([x,y]){
  const [l,t,r,b]=active.bounds,tolerance=13*active.width/frame.clientWidth;
  for(const [name,cx,cy] of [['nw',l,t],['ne',r,t],['sw',l,b],['se',r,b]])if(Math.abs(x-cx)<=tolerance && Math.abs(y-cy)<=tolerance)return name;
  return x>=l && x<=r && y>=t && y<=b?'move':'new';
}
frame.addEventListener('pointerdown',event=>{
  if(!active.ready || event.button!==0)return;
  event.preventDefault();frame.focus();const point=pointer(event);
  drag={point,bounds:active.bounds.slice(),dirty:active.dirty,mode:hit(point)};frame.setPointerCapture(event.pointerId);
});
frame.addEventListener('pointermove',event=>{
  if(!active.ready)return;
  const [x,y]=pointer(event),{width,height}=active;
  if(!drag){const mode=hit([x,y]);frame.style.cursor=mode==='move'?'move':mode==='new'?'crosshair':['nw','se'].includes(mode)?'nwse-resize':'nesw-resize';return;}
  const [sx,sy]=drag.point,[l,t,r,b]=drag.bounds;let next;
  if(drag.mode==='move'){
    const dx=clamp(x-sx,-l,width-r),dy=clamp(y-sy,-t,height-b);next=[l+dx,t+dy,r+dx,b+dy];
  }else if(drag.mode==='new'){
    const left=clamp(Math.min(sx,x),0,width-1),top=clamp(Math.min(sy,y),0,height-1);next=[left,top,Math.max(left+1,Math.max(sx,x)),Math.max(top+1,Math.max(sy,y))];
  }else{
    next=[l,t,r,b];
    if(drag.mode.includes('w'))next[0]=clamp(x,0,r-1);
    if(drag.mode.includes('e'))next[2]=clamp(x,l+1,width);
    if(drag.mode.includes('n'))next[1]=clamp(y,0,b-1);
    if(drag.mode.includes('s'))next[3]=clamp(y,t+1,height);
  }setBounds(next);
});
function finishDrag(){if(!drag)return;if(drag.mode==='new' && (active.bounds[2]-active.bounds[0]<4 || active.bounds[3]-active.bounds[1]<4)){active.dirty=drag.dirty;setBounds(drag.bounds,false);}drag=null;}
frame.addEventListener('pointerup',finishDrag);frame.addEventListener('pointercancel',finishDrag);
frame.addEventListener('keydown',event=>{
  if(!active.ready || !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
  event.preventDefault();const step=event.shiftKey?10:1,[l,t,r,b]=active.bounds;
  const dx=clamp(event.key==='ArrowLeft'?-step:event.key==='ArrowRight'?step:0,-l,active.width-r);
  const dy=clamp(event.key==='ArrowUp'?-step:event.key==='ArrowDown'?step:0,-t,active.height-b);
  setBounds([l+dx,t+dy,r+dx,b+dy]);
});
fields.forEach(field=>field.addEventListener('input',()=>{
  active.inputs=fields.map(input=>input.value);
  const next=active.inputs.map(value=>value.trim()===''?NaN:Number(value));
  active.validInputs=valid(next);active.dirty=true;
  $('bounds-error').textContent=active.validInputs?'':'Enter integer bounds inside the source frame, with right > left and bottom > top.';
  if(active.validInputs){active.bounds=next;active.dirty=hasChanges(active);}updateActions();draw();
}));
$('revert').addEventListener('click',()=>{setBounds(scaleBounds(active.saved.bounds_xyxy,active.saved.source_size_wh,[active.width,active.height]));status(`Restored the saved ${active.name} region.`);});
$('save').addEventListener('click',async()=>{
  const c=active;c.saving=true;updateActions();
  try{
    const response=await fetch(`/api/${c.name}/roi`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...c.source,source_size_wh:[c.width,c.height],bounds_xyxy:c.bounds})});
    const result=await response.json();if(!response.ok)throw new Error(result.error);
    c.saved=result;c.dirty=hasChanges(c);status(`Saved config/${c.name}_roi.json.`);
  }catch(error){status(`Save failed: ${error.message}`,true);}finally{c.saving=false;updateActions();}
});
function stopLocal(c){c.token++;clearTimeout(c.timer);if(c.liveImage){c.liveImage.close();c.liveImage=null;}}
function resizeSource(c,w,h){
  if(c.width!==w || c.height!==h){c.bounds=scaleBounds(c.bounds,[c.width,c.height],[w,h]);c.inputs=c.bounds.map(String);c.validInputs=true;}
  c.width=w;c.height=h;c.dirty=!c.validInputs || hasChanges(c);
}
function useSnapshot(c,disconnect=true){
  stopLocal(c);c.connection='reference';c.source={source_kind:'snapshot',camera_label:`${c.name} camera`,camera_browser_device_id:null};
  if(disconnect)cameraRequest(`/api/${c.name}/stop`).catch(()=>{});
  resizeSource(c,c.image.naturalWidth,c.image.naturalHeight);drawTile(c);if(c===active)syncControls();
}
async function pollCamera(c,token){
  try{
    const info=await (await fetch(`/api/${c.name}/status`)).json();if(token!==c.token)return;
    if(info.state==='error' || info.state==='off')throw new Error(info.error||`${c.title} disconnected.`);
    if(info.state==='live'){
      const response=await fetch(`/api/${c.name}/frame`);if(!response.ok)throw new Error(`No live ${c.name} frame is available. Reconnect the camera.`);
      const bitmap=await createImageBitmap(await response.blob());if(token!==c.token){bitmap.close();return;}
      const first=!c.liveImage;if(c.liveImage)c.liveImage.close();c.liveImage=bitmap;c.sequence=info.sequence;c.connection='live';c.cameraId=info.camera_unique_id;c.error=null;
      c.source={source_kind:'live',camera_driver:'native',camera_label:`${c.name} camera`,camera_browser_device_id:null};
      if(first){resizeSource(c,bitmap.width,bitmap.height);if(c===active){syncControls();status(`${c.title} connected automatically. Adjust its region, then Save ROI.`);}}
      drawTile(c);if(c===active)draw();
    }else{c.connection='starting';drawTile(c);if(c===active)syncControls();}
    c.timer=setTimeout(()=>pollCamera(c,token),100);
  }catch(error){if(token!==c.token)return;c.error=error.message;useSnapshot(c,false);if(c===active)status(error.message,true);}
}
async function connectCamera(c){
  stopLocal(c);c.connection='starting';c.error=null;drawTile(c);if(c===active){syncControls();status(`Connecting ${c.title.toLowerCase()}…`);}
  try{const response=await cameraRequest(`/api/${c.name}/start`);if(!response.ok)throw new Error('Could not start the camera.');if(!closing)pollCamera(c,c.token);}
  catch(error){c.error=error.message;useSnapshot(c,false);if(c===active)status(error.message,true);}
}
$('connect-camera').addEventListener('click',()=>connectCamera(active));
function renderCameraControls(){
  const c=active,s=c.controls,d=c.controlsDraft,blocked=c.controlsBusy||!s;
  $('read-camera').disabled=c.controlsBusy;
  $('auto-exposure').disabled=blocked;
  if(d){
    $('auto-exposure').checked=d.auto_exposure;
    for(const key of ['exposure','saturation','contrast']){
      const input=$(key),limits=s[key];
      input.min=limits.min;input.max=limits.max;input.step=Math.max(1,limits.step);input.value=d[key];
    }
    $('exposure-ms').min=s.exposure.min/10;$('exposure-ms').max=s.exposure.max/10;$('exposure-ms').step=Math.max(1,s.exposure.step)/10;
    $('exposure-ms').value=d.exposure/10;
    for(const key of ['saturation','contrast']){
      $(`${key}-number`).min=s[key].min;$(`${key}-number`).max=s[key].max;$(`${key}-number`).step=Math.max(1,s[key].step);$(`${key}-number`).value=d[key];
      $(`${key}-value`).textContent=`${d[key]} / ${s[key].max}`;
    }
    $('exposure-value').textContent=d.auto_exposure?'Automatic':`${(d.exposure/10).toFixed(1)} ms`;
  }else{
    $('exposure-value').textContent=$('saturation-value').textContent=$('contrast-value').textContent='—';
  }
  $('exposure').disabled=$('exposure-ms').disabled=blocked||Boolean(d?.auto_exposure);
  $('saturation').disabled=$('saturation-number').disabled=blocked;
  $('contrast').disabled=$('contrast-number').disabled=blocked;
  $('apply-camera').disabled=blocked||!session||closing;
  $('apply-camera').textContent=c.controlsBusy?'Working…':'Apply settings';
  $('camera-controls-status').textContent=c.controlsMessage;
  $('camera-controls-status').classList.toggle('failed',c.controlsError);
}
async function cameraSettings(c,apply=false){
  if(c.controlsBusy)return;
  c.controlsBusy=true;c.controlsError=false;c.controlsMessage=apply?'Applying and verifying camera settings…':'Reading hardware settings…';
  if(c===active)renderCameraControls();
  try{
    const response=await fetch(`/api/${c.name}/controls?session=${encodeURIComponent(sessionId)}`,apply?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c.controlsDraft)}:{cache:'no-store'});
    const values=await response.json();if(!response.ok)throw new Error(values.error);
    c.controls=values;c.controlsDraft={auto_exposure:values.auto_exposure,exposure:values.exposure.value,saturation:values.saturation.value,contrast:values.contrast.value};
    c.controlsMessage=apply?'Applied to camera and verified. All live views use these settings.':'Current hardware settings loaded. Changes apply when you click Apply settings.';
  }catch(error){c.controlsError=true;c.controlsMessage=error.message;}
  finally{c.controlsBusy=false;if(c===active)renderCameraControls();}
}
$('auto-exposure').addEventListener('change',()=>{active.controlsDraft.auto_exposure=$('auto-exposure').checked;active.controlsMessage='Unapplied changes. Click Apply settings to update this camera.';renderCameraControls();});
for(const [id,key,multiplier] of [['exposure','exposure',1],['exposure-ms','exposure',10],['saturation','saturation',1],['saturation-number','saturation',1],['contrast','contrast',1],['contrast-number','contrast',1]]){
  $(id).addEventListener('input',()=>{
    if(!$(id).value||!$(id).validity.valid){
      $('apply-camera').disabled=true;
      $('camera-controls-status').textContent='Enter a value within the displayed camera range and step.';
      $('camera-controls-status').classList.add('failed');return;
    }
    active.controlsDraft[key]=Math.round(Number($(id).value)*multiplier);
    active.controlsError=false;
    active.controlsMessage='Unapplied changes. Click Apply settings to update this camera.';
    renderCameraControls();
  });
}
$('apply-camera').addEventListener('click',()=>cameraSettings(active,true));
$('read-camera').addEventListener('click',()=>cameraSettings(active));
window.addEventListener('pagehide',()=>{
  closing=true;for(const c of Object.values(cameras))stopLocal(c);
  if(session)session.close();
  navigator.sendBeacon(`/api/session/close?session=${encodeURIComponent(sessionId)}`);
});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
new ResizeObserver(draw).observe(frame);
(async()=>{
  try{
    await Promise.all(Object.values(cameras).map(async c=>{
      c.image.src=`/assets/${c.name}.jpg`;
      const [response]=await Promise.all([fetch(`/api/${c.name}/roi`),c.image.decode()]);c.saved=await response.json();if(!response.ok)throw new Error(c.saved.error);
      c.width=c.image.naturalWidth;c.height=c.image.naturalHeight;c.bounds=scaleBounds(c.saved.bounds_xyxy,c.saved.source_size_wh,[c.width,c.height]);c.inputs=c.bounds.map(String);c.ready=true;c.dirty=hasChanges(c);$('select-'+c.name).disabled=false;drawTile(c);
    }));
    syncControls();status('Connecting front and wrist cameras automatically…');
    for(const c of Object.values(cameras)){c.connection='starting';drawTile(c);}
    syncControls();
    await openSession();if(closing)return;
    for(const c of Object.values(cameras))cameraSettings(c);
    const response=await cameraRequest('/api/cameras/start');if(!response.ok)throw new Error('Could not start the cameras.');
    if(!closing)for(const c of Object.values(cameras))pollCamera(c,c.token);
  }catch(error){status(`Cannot load workspace: ${error.message}`,true);}
})();

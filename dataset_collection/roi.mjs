// Match the Camera Setup app's scaling and exclusive right/bottom coordinates.
export function cropRect(roi,width,height){
  const size=roi?.source_size_wh,b=roi?.bounds_xyxy;
  if(!Array.isArray(size)||size.length!==2||!size.every(v=>Number.isInteger(v)&&v>0)||
     !Array.isArray(b)||b.length!==4||!b.every(Number.isInteger)||
     !(0<=b[0]&&b[0]<b[2]&&b[2]<=size[0]&&0<=b[1]&&b[1]<b[3]&&b[3]<=size[1]))
    throw new Error('Invalid saved ROI. Save a valid region in the Camera Setup app.');
  const clamp=(v,min,max)=>Math.max(min,Math.min(max,v));
  const p=b.map((v,i)=>Math.round(v*[width,height][i%2]/size[i%2]));
  const left=clamp(p[0],0,width-1),top=clamp(p[1],0,height-1);
  return [left,top,clamp(p[2],left+1,width)-left,clamp(p[3],top+1,height)-top];
}

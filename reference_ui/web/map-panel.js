/* Map viewport mathematics and independent published-API map client. */
(function(root) {
  'use strict';
  function worldToPixel(map, p) {
    const dx=p.x-map.origin.x, dy=p.y-map.origin.y, c=Math.cos(map.origin.yaw), s=Math.sin(map.origin.yaw);
    return {x:(c*dx+s*dy)/map.resolution,y:map.height-(-s*dx+c*dy)/map.resolution};
  }
  function pixelToWorld(map,p) {
    const x=p.x*map.resolution,y=(map.height-p.y)*map.resolution,c=Math.cos(map.origin.yaw),s=Math.sin(map.origin.yaw);
    return {x:map.origin.x+c*x-s*y,y:map.origin.y+s*x+c*y};
  }
  root.MapGeometry={worldToPixel,pixelToWorld};
  if(typeof document==='undefined') return;
  const canvas=document.getElementById('robot-map'),ctx=canvas.getContext('2d'),status=document.getElementById('map-message');
  const client=new RobotClient();
  let presets=[],map=null,img=null,scale=1,offset={x:0,y:0},mode=null,start=null,preview=null,busy=false,refreshing=false;
  function message(text){status.textContent=text;}
  function screen(p){const q=worldToPixel(map,p);return{x:q.x*scale+offset.x,y:q.y*scale+offset.y};}
  function world(p){return pixelToWorld(map,{x:(p.x-offset.x)/scale,y:(p.y-offset.y)/scale});}
  function point(e){const r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)*canvas.width/r.width,y:(e.clientY-r.top)*canvas.height/r.height};}
  function arrow(p,color){const q=screen(p),a=map.origin.yaw-p.yaw;ctx.strokeStyle=color;ctx.lineWidth=3;ctx.beginPath();ctx.arc(q.x,q.y,6,0,Math.PI*2);ctx.moveTo(q.x,q.y);ctx.lineTo(q.x+24*Math.cos(a),q.y+24*Math.sin(a));ctx.stroke();}
  function draw(){ctx.clearRect(0,0,canvas.width,canvas.height);if(!map||!img)return;ctx.drawImage(img,offset.x,offset.y,map.width*scale,map.height*scale);if(map.robot_pose)arrow(map.robot_pose,map.robot_pose.fresh?'#168b64':'#888');for(const p of presets)arrow(p.position,'#777');if(map.target)arrow(map.target,'#2869bc');if(preview)arrow(preview,'#d76100');}
  function cancel(){preview=null;start=null;mode=null;document.getElementById('map-apply').disabled=true;draw();}
  for(const id of ['map-locate','map-go']) document.getElementById(id).onclick=()=>{cancel();mode=id==='map-go'?'go':'locate';message('Нажмите на позицию и протяните стрелку в нужном направлении.');};
  document.getElementById('map-cancel').onclick=cancel;
  for(const [id,factor] of [['map-plus',1.3],['map-minus',1/1.3]])document.getElementById(id).onclick=()=>{const next=Math.max(.02,Math.min(20,scale*factor)),r=next/scale;offset={x:canvas.width/2+(offset.x-canvas.width/2)*r,y:canvas.height/2+(offset.y-canvas.height/2)*r};scale=next;draw();};
  document.getElementById('map-center').onclick=()=>{if(!map?.robot_pose)return;const q=worldToPixel(map,map.robot_pose);offset={x:canvas.width/2-q.x*scale,y:canvas.height/2-q.y*scale};draw();};
  canvas.onpointerdown=e=>{if(!map||!img||busy)return;canvas.setPointerCapture(e.pointerId);start={point:point(e),offset:{...offset},version:map.version};preview=null;};
  canvas.onpointermove=e=>{if(!start)return;const p=point(e);if(!mode){offset={x:start.offset.x+p.x-start.point.x,y:start.offset.y+p.y-start.point.y};}else{const a=world(start.point),b=world(p);preview={...a,yaw:Math.atan2(b.y-a.y,b.x-a.x)};}draw();};
  canvas.onpointerup=e=>{if(!start)return;const p=point(e);if(mode){if(Math.hypot(p.x-start.point.x,p.y-start.point.y)<12){preview=null;message('Протяните стрелку, чтобы указать направление.');}else{const a=world(start.point),b=world(p);preview={...a,yaw:Math.atan2(b.y-a.y,b.x-a.x)};message(mode==='go'?'Подтвердите поездку к выбранной позе.':'Подтвердите фактическое положение робота.');}document.getElementById('map-apply').disabled=!preview;}start=null;draw();};
  canvas.onpointercancel=cancel;
  document.getElementById('map-apply').onclick=async()=>{if(!preview||busy)return;busy=true;const body={pose:preview,map_version:map.version};const path=mode==='go'?'/api/v1/missions/go-to-pose':'/api/v1/localization/reset';if(mode!=='go')body.confirm=true;let r=await client.request('POST',path,body);if(r.ok&&r.data.id)r=await client.awaitAcceptance(r.data);message(r.ok?(r.data.costmaps_cleared===false?'Положение отправлено, но очистка карт препятствий не подтверждена. Проверьте наблюдаемое положение.':'Команда принята. Следите за состоянием робота.'):r.data.message||'Команда отклонена');busy=false;cancel();};
  async function refresh(){if(refreshing)return;refreshing=true;try{if(document.getElementById('menu-overlay').hidden){cancel();return;}if(document.getElementById('section-map').hidden)return;const r=await client.request('GET','/api/v1/map');if(!r.ok||!r.data.available){message('Нет карты');return;}const changed=map?.version!==r.data.version;map=r.data;if(changed){cancel();img=null;const list=await client.request('GET','/api/v1/localization/presets');presets=(list.data.presets||[]).map(p=>{const q=p.pose.pose.orientation;return{id:p.id,position:{x:p.pose.pose.position.x,y:p.pose.pose.position.y,yaw:Math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))}};});const holder=document.getElementById('map-presets');holder.replaceChildren();for(const p of presets){const button=document.createElement('button');button.className='btn';button.textContent=p.id;button.onclick=()=>{mode='locate';preview=p.position;document.getElementById('map-apply').disabled=false;message('Подтвердите положение робота: '+p.id);draw();};holder.append(button);}const version=map.version;const imageResponse=await fetch('/api/v1/map/image?v='+encodeURIComponent(version),{cache:'no-store'});if(!imageResponse.ok||imageResponse.headers.get('ETag')?.replaceAll('"','')!==version){map=null;img=null;message('Карта изменилась. Обновление…');return;}const url=URL.createObjectURL(await imageResponse.blob());const nextImage=new Image();nextImage.onload=()=>{URL.revokeObjectURL(url);if(map?.version!==version)return;img=nextImage;scale=Math.min(canvas.width/map.width,canvas.height/map.height);offset={x:0,y:0};draw();};nextImage.onerror=()=>{URL.revokeObjectURL(url);map=null;img=null;};nextImage.src=url;}if(!mode&&!busy)message(!map.robot_pose?'Положение неизвестно':!map.robot_pose.fresh?'Данные устарели':'Карта актуальна');draw();}finally{refreshing=false;}}
  setInterval(refresh,1000);refresh();
})(typeof window==='undefined'?globalThis:window);

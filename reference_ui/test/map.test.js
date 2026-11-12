const test=require('node:test'),assert=require('node:assert/strict');
require('../web/map-panel.js');
test('rotated map origin round trips world and image coordinates',()=>{
 const map={origin:{x:2,y:3,yaw:Math.PI/2},width:100,height:80,resolution:.05};
 const point={x:1.5,y:4};const pixel=MapGeometry.worldToPixel(map,point),back=MapGeometry.pixelToWorld(map,pixel);
 assert.ok(Math.abs(point.x-back.x)<1e-9);assert.ok(Math.abs(point.y-back.y)<1e-9);
 assert.ok(Math.abs(pixel.x-20)<1e-9);assert.ok(Math.abs(pixel.y-70)<1e-9);
});
test('quick localization uses the three named locations and their actual orientation',()=>{
 const location=(id,x,y,yaw,frame='map')=>({id,pose:{header:{frame_id:frame},pose:{
  position:{x,y,z:0},orientation:{x:0,y:0,z:Math.sin(yaw/2),w:Math.cos(yaw/2)}
 }}});
 const points=MapGeometry.quickPresets([
  location('charging',-2.275,.780,-2.881),location('home',5,5,0),
  location('parking',20.686,1.027,-.218),location('expo_meeting',-.1868,-.0299,0)
 ],'map');
 assert.deepEqual(points.map(p=>p.label),['Начало экскурсии','Стоянка','Подзарядка']);
 assert.deepEqual(points.map(p=>p.id),['expo_meeting','parking','charging']);
 assert.equal(points[1].position.x,20.686);
 assert.ok(Math.abs(points[1].position.yaw+.218)<1e-9);
 assert.ok(Math.abs(points[2].position.yaw+2.881)<1e-9);
 assert.equal(MapGeometry.quickPresets([location('parking',0,0,0,'other')],'map')[1].position,null);
 assert.ok(MapGeometry.quickPresets([],'map').every(p=>p.position===null));
});

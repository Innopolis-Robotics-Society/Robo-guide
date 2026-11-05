const test=require('node:test'),assert=require('node:assert/strict');
require('../web/map-panel.js');
test('rotated map origin round trips world and image coordinates',()=>{
 const map={origin:{x:2,y:3,yaw:Math.PI/2},width:100,height:80,resolution:.05};
 const point={x:1.5,y:4};const pixel=MapGeometry.worldToPixel(map,point),back=MapGeometry.pixelToWorld(map,pixel);
 assert.ok(Math.abs(point.x-back.x)<1e-9);assert.ok(Math.abs(point.y-back.y)<1e-9);
 assert.ok(Math.abs(pixel.x-20)<1e-9);assert.ok(Math.abs(pixel.y-70)<1e-9);
});

const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');

async function panel(){
 const elements=new Map(),posts=[];
 const state={available:true,presetsAvailable:true,version:'map-one',imageVersion:'map-one'};
 const ids=['robot-map','map-message','map-apply','map-cancel','map-locate','map-go','map-plus',
  'map-minus','map-center','menu-overlay','section-map','map-preset-start','map-preset-parking','map-preset-charging'];
 const ctx=new Proxy({}, {get:()=>()=>{}});
 for(const id of ids)elements.set(id,{disabled:false,hidden:false,width:900,height:480,getContext:()=>ctx});
 const locations=['expo_meeting','parking','charging'].map((id,i)=>({id,pose:{header:{frame_id:'map'},
  pose:{position:{x:i,y:i,z:0},orientation:{x:0,y:0,z:0,w:1}}}}));
 let tick;
 const sandbox={document:{getElementById:id=>elements.get(id)},
  RobotClient:class{
   async request(method,path,body){
    if(method==='POST'){posts.push({path,body:JSON.parse(JSON.stringify(body))});return {ok:true,data:{}};}
    if(path.endsWith('/presets'))return {ok:state.presetsAvailable,data:{presets:locations}};
    return {ok:true,data:{available:state.available,version:state.version,frame:'map',width:20,height:20,
      resolution:1,origin:{x:0,y:0,yaw:0},robot_pose:null}};
   }
  },
  Image:class{set src(value){this.onload();}},
  URL:{createObjectURL:()=>'',revokeObjectURL:()=>{}},
  fetch:async()=>({ok:true,headers:{get:()=>state.imageVersion},blob:async()=>({})}),
  setInterval:fn=>{tick=fn;}
 };
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-panel.js'),'utf8'),sandbox);
 await new Promise(setImmediate);
 return {elements,posts,state,refresh:()=>tick()};
}

test('quick positions always request localization, even after selecting travel mode',async()=>{
 const {elements,posts}=await panel();
 for(const [button,preset] of [['map-preset-start','expo_meeting'],['map-preset-parking','parking'],['map-preset-charging','charging']]){
  const before=posts.length;
  assert.equal(elements.get(button).disabled,false);
  elements.get('map-go').onclick();
  elements.get(button).onclick();
  assert.equal(posts.length,before,'selecting a point only previews it');
  await elements.get('map-apply').onclick();
  assert.deepEqual(posts[before],{path:'/api/v1/localization/reset',
   body:{map_version:'map-one',preset_id:preset,confirm:true}});
 }
});

test('loss of map cancels the preview and disables quick positions',async()=>{
 const {elements,posts,state,refresh}=await panel();
 elements.get('map-preset-parking').onclick();
 state.available=false;await refresh();
 assert.equal(elements.get('map-preset-parking').disabled,true);
 assert.equal(elements.get('map-apply').disabled,true);
 await elements.get('map-apply').onclick();assert.equal(posts.length,0);
});

test('changed map version invalidates old selection and rejects mismatched image',async()=>{
 const {elements,posts,state,refresh}=await panel();
 elements.get('map-preset-start').onclick();state.version='map-two';await refresh();
 assert.equal(elements.get('map-preset-start').disabled,true);
 await elements.get('map-apply').onclick();assert.equal(posts.length,0);
 state.imageVersion='map-two';await refresh();
 assert.equal(elements.get('map-preset-start').disabled,false);
});

test('unavailable preset service is retried without requiring another map version',async()=>{
 const {elements,state,refresh}=await panel();
 state.available=false;await refresh();
 state.available=true;state.presetsAvailable=false;await refresh();
 assert.equal(elements.get('map-preset-charging').disabled,true);
 state.presetsAvailable=true;await refresh();
 assert.equal(elements.get('map-preset-charging').disabled,false);
});

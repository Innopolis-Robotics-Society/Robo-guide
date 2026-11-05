const test=require('node:test'), assert=require('node:assert/strict');
const RfidWait=require('../web/rfid-wait.js');
const empty={ok:false,status:401,data:{reason:'rfid_no_card'}};
test('waits 30 seconds, uses fresh nonces and never overlaps requests',async()=>{
 let clock=0,nonces=0,attempts=0;
 const wait=new RfidWait(async(method,path,body)=>{
  if(path.endsWith('challenge'))return {ok:true,data:{nonce:String(++nonces),backends:['rfid']}};
  assert.equal(body.nonce,String(++attempts));return empty;
 },()=>clock,async ms=>{clock+=ms;});
 assert.equal((await wait.run()).kind,'timeout');assert.equal(clock,30000);assert.equal(attempts,60);
});
test('stops on success or bad credentials',async()=>{
 for(const ok of [true,false]){
  let attempts=0;
  const wait=new RfidWait(async(method,path)=>path.endsWith('challenge')?
   {ok:true,data:{nonce:'n',backends:['rfid']}}:
   (++attempts,{ok,data:{}}));
  assert.equal((await wait.run()).kind,ok?'success':'error');assert.equal(attempts,1);
 }
});
test('cancelled in-flight success is logged out and does not open menu',async()=>{
 let loggedOut=false,wait;
 wait=new RfidWait(async(method,path)=>{
  if(path.endsWith('challenge'))return {ok:true,data:{nonce:'n',backends:['rfid']}};
  if(path.endsWith('logout')){loggedOut=true;return {ok:true,data:{}};}
  wait.cancel();return {ok:true,data:{operator:'op_test'}};
 });
 assert.equal((await wait.run()).kind,'cancelled');assert.equal(loggedOut,true);
});

/* Bounded RFID polling via the published API; no serial or DOM dependencies. */
(function(root){
 'use strict';
 class RfidWait {
  constructor(request, now=()=>performance.now(), sleep=ms=>new Promise(r=>setTimeout(r,ms))) {
   this.request=request;this.now=now;this.sleep=sleep;this.cancelled=false;
  }
  cancel(){this.cancelled=true;}
  async run(){
   const deadline=this.now()+30000;
   while(!this.cancelled&&this.now()<deadline){
    const challenge=await this.request('POST','/api/v1/auth/challenge');
    if(!challenge.ok)return {kind:'error',response:challenge};
    if(this.cancelled||this.now()>=deadline)break;
    if(!(challenge.data.backends||[]).includes('rfid'))return {kind:'unavailable'};
    const response=await this.request('POST','/api/v1/auth/verify',{nonce:challenge.data.nonce,backend:'rfid'});
    if(this.cancelled||this.now()>=deadline){
     // A successful in-flight request may already have set an HttpOnly cookie.
     if(response.ok)await this.request('POST','/api/v1/auth/logout');
     break;
    }
    if(response.ok)return {kind:'success',response};
    if(response.data.reason!=='rfid_no_card')return {kind:'error',response};
    await this.sleep(Math.min(500,Math.max(0,deadline-this.now())));
   }
   return {kind:this.cancelled?'cancelled':'timeout'};
  }
 }
 root.RfidWait=RfidWait;
 if(typeof module!=='undefined')module.exports=RfidWait;
})(globalThis);

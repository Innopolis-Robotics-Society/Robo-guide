const test = require('node:test');
const assert = require('node:assert/strict');
const {resolveChunkId, selectMedia} = require('../web/media-select.js');
test('select exact chunk, fallback general media, invalid index', () => {
  const general = {chunk_id:'',path:'a.jpg'};
  const exact = {chunk_id:'intro',path:'b.mp4'};
  assert.equal(resolveChunkId(['intro'],0),'intro');
  for (const index of [-1,1,100]) assert.equal(resolveChunkId(['intro'],index),null);
  assert.deepEqual(selectMedia([general,exact],'intro'),[exact]);
  assert.deepEqual(selectMedia([general,exact],'missing'),[general]);
  assert.deepEqual(selectMedia([general,exact],null),[general]);
  assert.deepEqual(selectMedia([exact],null),[]);
});
require('../web/api-client.js');
test('client uses session credentials, idempotency key and error envelope', async () => {
  let sent;
  global.fetch = async (url,options) => {
    sent={url,...options};
    return {ok:false,status:409,json:async()=>({error:{code:'invalid_state',message:'busy',request_id:'r'}})};
  };
  const result = await new RobotClient().request('POST','/api/v1/missions',{tour_id:'t'},'stable-key');
  assert.equal(sent.credentials,'include');
  assert.equal(sent.headers['Idempotency-Key'],'stable-key');
  assert.equal(result.data.message,'busy');
  assert.equal(result.data.reason,'busy');
  assert.equal(result.status,409);
});
test('accepted HTTP request reports later ROS rejection, not success', async () => {
  const client = new RobotClient();
  const response = await client.awaitAcceptance({id:'a',instance_id:'i',status:'rejected',result:{message:'busy'}});
  assert.equal(response.ok,false);
  assert.equal(response.data.message,'busy');
  const unknown = await client.awaitAcceptance({id:'a',instance_id:'i',status:'pending'},0);
  assert.equal(unknown.ok,false);
  assert.equal(unknown.data.message,'outcome_unknown');
});
test('network failure retains idempotency key on retry', async () => {
  global.crypto = require('node:crypto').webcrypto;
  const client = new RobotClient();
  const keys=[];
  global.fetch=async (url,options)=> {keys.push(options.headers['Idempotency-Key']);throw new Error('lost response');};
  await client.request('POST','/api/v1/missions',{tour_id:'t'});
  await client.request('POST','/api/v1/missions',{tour_id:'t'});
  assert.equal(keys.length,2);
  assert.equal(keys[0],keys[1]);
});
test('pending operation observes ROS acceptance and detects API restart', async () => {
  const client = new RobotClient();
  client.request = async () => ({ok:true,data:{instance_id:'i',operations:[{id:'a',instance_id:'i',status:'running'}]}});
  const accepted = await client.awaitAcceptance({id:'a',instance_id:'i',status:'pending'});
  assert.equal(accepted.ok,true);
  client.request = async () => ({ok:true,data:{instance_id:'new',operations:[]}});
  const restarted = await client.awaitAcceptance({id:'a',instance_id:'i',status:'pending'});
  assert.equal(restarted.ok,false);
  assert.equal(restarted.data.message,'outcome_unknown');
});

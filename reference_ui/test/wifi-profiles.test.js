const test=require('node:test'),assert=require('node:assert/strict');
const WifiProfiles=require('../web/wifi-profiles.js');
const profiles=[{id:'old',ssid:'phone'},{id:'active',ssid:'phone'},{id:'offline',ssid:'offline'}];
const networks=[{ssid:'phone',strength:20},{ssid:'guest',strength:70},{ssid:'phone',strength:90}];
test('one row per visible network despite old duplicates, strongest signal, no offline rows',()=>{
 const rows=WifiProfiles.rows({profiles,networks});
 assert.deepEqual(rows,[
  {ssid:'phone',network:networks[2],saved:true},
  {ssid:'guest',network:networks[1],saved:false}
 ]);
 assert.equal(profiles.length,3);
});
test('network rows do not expose profile names, identifiers or a selection',()=>{
 const rows=WifiProfiles.rows({profiles,networks,current_profile_id:'active'});
 assert.deepEqual(Object.keys(rows[0]).sort(),['network','saved','ssid']);
 assert.doesNotMatch(JSON.stringify(rows),/active|old/);
});
test('empty discovery and missing saved settings are supported',()=>{
 assert.deepEqual(WifiProfiles.rows({profiles}),[]);
 assert.equal(WifiProfiles.rows({networks})[0].saved,false);
});
test('connected network shows its own IP and hides stale IPs when disconnected',()=>{
 assert.equal(WifiProfiles.connectionText({current_ssid:'iPhone',ipv4_addresses:['172.20.10.5']}),
  'Подключено: iPhone · IP: 172.20.10.5');
 assert.equal(WifiProfiles.connectionText({current_ssid:'iPhone'}),'Подключено: iPhone · IP: ожидается');
 assert.equal(WifiProfiles.connectionText({current_ssid:null,ipv4_addresses:['172.20.10.5']}),'Нет подключения');
});

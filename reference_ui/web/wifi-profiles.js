/* One row per discovered network; saved settings stay an implementation detail. */
(function(root){
'use strict';
function rows(data){
 const strongest=new Map();
 for(const network of data.networks||[]){
  if(!strongest.has(network.ssid)||network.strength>strongest.get(network.ssid).strength)strongest.set(network.ssid,network);
 }
 return Array.from(strongest.values(),network=>{
  return {ssid:network.ssid,network,saved:(data.profiles||[]).some(p=>p.ssid===network.ssid)};
 }).sort((a,b)=>b.network.strength-a.network.strength||a.ssid.localeCompare(b.ssid));
}
function connectionText(data){
 if(!data.current_ssid)return 'Нет подключения';
 const addresses=(data.ipv4_addresses||[]).join(', ');
 return 'Подключено: '+data.current_ssid+' · IP: '+(addresses||'ожидается');
}
const api={rows,connectionText};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
else root.WifiProfiles=api;
})(globalThis);

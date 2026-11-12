/* Host settings use the public API; the optional sound check requires ROS speech. */
(function(){
'use strict';
const root=document.getElementById('system-settings'),client=new RobotClient();
root.innerHTML=`<h2>Экран с глазами</h2>
<label class="switch-row"><input type="checkbox" id="face-cursor-visible" disabled><span>Показывать курсор</span></label>
<p id="face-cursor-message" role="status"></p>
<h2>Звук</h2><label>Громкость <input id="output-volume" type="range" min="0" max="100" value="0"></label>
<label><input type="checkbox" id="output-muted">Без звука</label><button class="btn" id="audio-test">Проверить звук</button><p id="audio-test-message" role="status"></p><p id="audio-message" role="status"></p>
<h2>Wi-Fi</h2><button class="btn" id="wifi-scan">Обновить сети</button><div id="wifi-list"></div>
<div class="row"><button class="btn" id="wifi-manual">Другая сеть…</button><button class="btn" id="wifi-disconnect">Отключить</button></div>
<p id="wifi-message" role="status"></p>
<dialog id="wifi-dialog" aria-labelledby="wifi-dialog-title"><form id="wifi-form">
<h2 id="wifi-dialog-title">Подключение к Wi-Fi</h2>
<label>Название сети <input id="wifi-ssid" maxlength="32" autocomplete="off" required></label>
<p id="wifi-password-hint"></p>
<label id="wifi-password-label">Пароль <input id="wifi-password" type="password" autocomplete="new-password"></label>
<label id="wifi-show-label"><input id="wifi-show" type="checkbox">Показать пароль</label>
<label id="wifi-hidden-label"><input id="wifi-hidden" type="checkbox">Скрытая сеть</label>
<div class="row"><button class="btn" type="submit" id="wifi-connect">Подключить</button><button class="btn" type="button" id="wifi-cancel">Отмена</button></div>
</form></dialog>`;
const el=id=>document.getElementById(id);let timer=null,pending=null,sending=false;
let cursorVisible=false;
async function loadCursor(){
 const r=await client.request('GET','/api/v1/display/face-cursor');
 el('face-cursor-visible').disabled=!r.ok;
 if(r.ok){cursorVisible=r.data.visible;el('face-cursor-visible').checked=cursorVisible;el('face-cursor-message').textContent='';}
 else el('face-cursor-message').textContent=r.status===401?'Войдите в меню оператора':'Настройка курсора недоступна';
}
el('face-cursor-visible').onchange=async()=>{
 const input=el('face-cursor-visible');input.disabled=true;
 try{
  const r=await client.request('POST','/api/v1/display/face-cursor',{visible:input.checked});
  if(r.ok)cursorVisible=r.data.visible;
  input.checked=cursorVisible;
  el('face-cursor-message').textContent=r.ok?'Сохранено':r.status===401?'Войдите в меню оператора':'Не удалось сохранить настройку курсора';
 }finally{input.disabled=false;}
};
function closeWifi(){el('wifi-dialog').close();clearTimeout(revealTimer);el('wifi-password').value='';el('wifi-password').type='password';el('wifi-show').checked=false;}
function openWifi(ssid='',saved=false,security=null){
 el('wifi-ssid').value=ssid;el('wifi-ssid').readOnly=!!ssid;
 el('wifi-password').value='';el('wifi-password').type='password';el('wifi-show').checked=false;
 el('wifi-hidden').checked=!ssid;el('wifi-hidden-label').hidden=!!ssid;
 el('wifi-password-label').hidden=security==='open';el('wifi-show-label').hidden=security==='open';
 el('wifi-password-hint').textContent=security==='open'?'Открытая сеть — пароль не нужен.':saved?
  'Сеть сохранена. Оставьте пароль пустым или введите новый.':'Введите пароль сети.';
 el('wifi-password').placeholder=saved?'Использовать сохранённый пароль':'';
 el('wifi-dialog').showModal();(ssid&&security!=='open'?el('wifi-password'):el('wifi-ssid')).focus();
}
el('wifi-cancel').onclick=closeWifi;
el('wifi-dialog').addEventListener('cancel',event=>{event.preventDefault();closeWifi();});
el('wifi-manual').onclick=()=>openWifi();
let revealTimer=null;el('wifi-show').onchange=()=>{clearTimeout(revealTimer);el('wifi-password').type=el('wifi-show').checked?'text':'password';if(el('wifi-show').checked)revealTimer=setTimeout(()=>{el('wifi-show').checked=false;el('wifi-password').type='password';},10000);};
async function sendVolume(){if(sending||!pending)return;sending=true;const body=pending;pending=null;const r=await client.request('POST','/api/v1/audio/output',body);el('audio-message').textContent=r.ok?('Применённая громкость: '+Math.round(r.data.volume)+'%'+(r.data.muted?' · без звука':'')):'Не удалось применить громкость. Проверьте устройство звука.';sending=false;if(pending)sendVolume();}
function paintVolume(){const p=Number(el('output-volume').value);el('output-volume').style.setProperty('--volume-fill',`calc(${p}% + ${14-0.28*p}px)`);}
paintVolume();
el('audio-test').onclick=async()=>{
 const button=el('audio-test'),message=el('audio-test-message');button.disabled=true;
 message.textContent='Проверка звука…';
 try {
  const r=await client.request('POST','/api/v1/speech',{text:'Как слышно?'});
  if(!r.ok){message.textContent=r.status===401?'Войдите в меню оператора':'Не удалось включить проверку звука. Проверьте доступность речи.';return;}
  let op=r.data;const deadline=Date.now()+30000;
  while(['pending','running'].includes(op.status)&&Date.now()<deadline){
   await new Promise(resolve=>setTimeout(resolve,500));
   const result=await client.request('GET','/api/v1/operations/'+op.id);
   if(!result.ok){message.textContent='Результат проверки звука неизвестен';return;}
   op=result.data;
  }
  await client.awaitAcceptance(op,0);
  message.textContent=op.status==='succeeded'?'Проверка звука завершена':op.status==='failed'?'Не удалось воспроизвести фразу':'Завершение проверки звука не подтверждено';
 } finally {button.disabled=false;}
};
function volume(){paintVolume();pending={volume:Number(el('output-volume').value),muted:el('output-muted').checked};clearTimeout(timer);timer=setTimeout(sendVolume,200);}
el('output-volume').oninput=volume;el('output-volume').onchange=()=>{volume();clearTimeout(timer);sendVolume();};el('output-muted').onchange=()=>{volume();sendVolume();};
function showNetworks(data){
 const list=el('wifi-list');list.replaceChildren();
 const current=document.createElement('p');current.textContent=WifiProfiles.connectionText(data);list.append(current);
 const table=document.createElement('table');table.className='wifi-table';
 const head=table.createTHead().insertRow();
 for(const title of ['Название сети','Подключить','Забыть','Сигнал']){const th=document.createElement('th');th.scope='col';th.textContent=title;head.append(th);}
 const body=table.createTBody(),rows=WifiProfiles.rows(data);
 for(const {saved,network,ssid} of rows){
  const row=body.insertRow(),name=row.insertCell();name.textContent=ssid||'Скрытая сеть';
  const detail=document.createElement('small');detail.textContent=[data.current_ssid===ssid?'Подключено':'',saved?'Сохранена':'',network?.security||''].filter(Boolean).join(' · ');name.append(detail);
  const connect=document.createElement('button');connect.className='btn';connect.textContent='Подключить';connect.setAttribute('aria-label','Подключить '+ssid);
  connect.disabled=['enterprise','unsupported'].includes(network?.security);
  connect.onclick=()=>openWifi(ssid,saved,network?.security);row.insertCell().append(connect);
  const forgetCell=row.insertCell();
  if(saved){const forget=document.createElement('button');forget.className='btn';forget.textContent='Забыть';forget.setAttribute('aria-label','Забыть '+ssid);
   forget.onclick=()=>{if(confirm('Забыть сеть «'+ssid+'»?'))wifi('forget',{ssid});};forgetCell.append(forget);
  }else forgetCell.textContent='—';
  row.insertCell().textContent=network?network.strength+'%':'—';
 }
 list.append(table);
 if(!rows.length){const empty=document.createElement('p');empty.textContent='Сети не найдены. Нажмите «Обновить сети».';list.append(empty);}
}
async function wifi(action,body={}){el('wifi-message').textContent='Выполняется…';const r=await client.request(action?'POST':'GET','/api/v1/wifi'+(action?'/'+action:''),action?body:undefined);el('wifi-message').textContent=r.ok?'Настройки сети получены':(r.status===401?'Войдите в меню оператора':r.status===409?'Дождитесь остановки робота или завершения подключения':'Сеть недоступна. Проверьте адаптер Wi-Fi.');if(r.ok&&r.data.id){let op=r.data;while(['pending','running'].includes(op.status)){await new Promise(resolve=>setTimeout(resolve,1000));const poll=await client.request('GET','/api/v1/operations/'+op.id);if(!poll.ok){el('wifi-message').textContent='Исход подключения неизвестен';return poll;}op=poll.data;el('wifi-message').textContent=({connecting:'Подключение…',authenticating:'Проверка пароля…',obtaining_address:'Получение адреса…',restoring:'Восстановление прежней сети…'})[op.feedback?.phase]||'Подключение…';}await client.awaitAcceptance(op,0);el('wifi-message').textContent=op.status==='succeeded'?'Подключено':op.result?.restored_previous?'Не удалось подключиться; прежняя сеть восстановлена':'Подключение не подтверждено';const state=await client.request('GET','/api/v1/wifi');if(state.ok)showNetworks(state.data);}else if(r.ok)showNetworks(r.data);return r;}
async function scanWifi(){
 const button=el('wifi-scan');if(button.disabled)return;button.disabled=true;
 try{await wifi('scan');}finally{button.disabled=false;}
}
el('wifi-scan').onclick=scanWifi;el('wifi-disconnect').onclick=()=>wifi('disconnect');
el('wifi-form').onsubmit=async event=>{event.preventDefault();
 const password=el('wifi-password').value;
 const body={ssid:el('wifi-ssid').value,password,hidden:el('wifi-hidden').checked};
 closeWifi();await wifi('connect',body);
};
let visible=false;setInterval(async()=>{const show=!document.getElementById('section-settings').hidden&&!document.getElementById('menu-overlay').hidden;if(!show){if(visible)closeWifi();visible=false;return;}if(visible)return;visible=true;await loadCursor();const r=await client.request('GET','/api/v1/audio/output');if(r.ok){el('output-volume').value=r.data.volume;paintVolume();el('output-muted').checked=r.data.muted;}else el('audio-message').textContent='Устройство звука недоступно';await wifi();await scanWifi();},500);
})();

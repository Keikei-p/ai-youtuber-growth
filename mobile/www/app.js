import { Capacitor } from '@capacitor/core';
import { SecureStorage } from '@aparajita/capacitor-secure-storage';

const KEY_URL='mirai-base-url';
const KEY_TOKEN='pairing-token';
let secureReady=false;
let cachedToken='';

const $=id=>document.getElementById(id);
const setupCard=$('setupCard');
const dashboard=$('dashboard');
const baseUrl=$('baseUrl');
const token=$('token');
const toast=$('toast');
let state=null;
let timer=null;

function notify(message){
  toast.textContent=String(message||'');
  toast.classList.add('show');
  setTimeout(()=>toast.classList.remove('show'),2600);
}

async function ensureSecureStorage(){
  if(secureReady) return;
  await SecureStorage.setKeyPrefix('mirai_');
  secureReady=true;
}

async function loadSecureToken(){
  await ensureSecureStorage();
  cachedToken=(await SecureStorage.getItem(KEY_TOKEN))||'';
  return cachedToken;
}

async function saveSecureToken(value){
  await ensureSecureStorage();
  await SecureStorage.setItem(KEY_TOKEN,String(value||''));
  cachedToken=String(value||'');
}

async function removeSecureToken(){
  await ensureSecureStorage();
  await SecureStorage.removeItem(KEY_TOKEN);
  cachedToken='';
}

function connection(){
  return {
    url:(localStorage.getItem(KEY_URL)||'').replace(/\/+$/,''),
    token:cachedToken
  };
}

async function request(path,{method='GET',body=null}={}){
  const cfg=connection();
  if(!cfg.url||!cfg.token) throw new Error('接続設定がありません');
  const headers={'Authorization':'Bearer '+cfg.token};
  if(body!==null) headers['Content-Type']='application/json';
  const response=await fetch(cfg.url+path,{
    method,headers,body:body===null?undefined:JSON.stringify(body)
  });
  let data={};
  try{data=await response.json()}catch(e){}
  if(!response.ok) throw new Error(data.message||('HTTP '+response.status));
  return data;
}

function escapeHtml(v){
  return String(v??'').replace(/[&<>"']/g,m=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[m]));
}

function render(){
  const ap=state&&state.autopilot||{};
  const running=Boolean(ap.armed&&state.automation_enabled&&state.auto_upload_enabled&&!ap.runtime_cancel_requested);
  $('autopilotTitle').textContent=running?'🟢 完全自動運用中':(ap.armed?'🟡 復旧・確認中':'停止中');
  $('autopilotDetail').textContent=ap.armed
    ? ('毎日'+Number(ap.posts_per_day||state.posts_per_day||0)+'本 / '+String(ap.post_times||state.post_times||'-')+' / '+String(state.privacy||'-'))
    : '開始すると制作・投稿・分析・改善を自動で続けます。';
  $('autopilotButton').textContent=ap.armed?'完全自動運用を停止':'完全自動運用開始';
  $('autopilotButton').className=ap.armed?'danger wide':'primary';

  const next=state.next_queue||{};
  $('nextPost').textContent=next.scheduled_for?String(next.scheduled_for).replace('T',' ').slice(5,16):'予定なし';
  $('nextPostTitle').textContent=next.title||'';

  const last=state.last_post||{};
  const labels={verified:'成功',recovery_wait:'再試行待ち',attention:'要確認',blocked:'停止',attempting:'投稿中',idle:'待機'};
  $('lastPost').textContent=labels[last.status]||last.status||'未実行';
  $('lastPostDetail').textContent=last.detail||'';

  const issues=state.problems||[];
  $('problems').innerHTML=issues.length
    ? issues.map(x=>'<div class="problem">'+escapeHtml(x.detail||x.code||'要確認')+'</div>').join('')
    : '<p class="muted">大きな問題は検出されていません。</p>';

  $('connectionLabel').textContent=connection().url;
}

async function refresh(){
  state=await request('/api/mobile/status');
  render();
}

function showDashboard(){setupCard.hidden=true;dashboard.hidden=false;}
function showSetup(){
  setupCard.hidden=false;dashboard.hidden=true;
  const cfg=connection();
  baseUrl.value=cfg.url;
  token.value='';
}

$('saveConnection').addEventListener('click',async()=>{
  const url=baseUrl.value.trim().replace(/\/+$/,'');
  const secret=token.value.trim();
  if(!/^https:\/\//i.test(url)){notify('HTTPSの接続URLを入力してください');return;}
  if(secret.length<20){notify('接続コードを確認してください');return;}
  localStorage.setItem(KEY_URL,url);
  await saveSecureToken(secret);
  try{showDashboard();await refresh();notify('ミライへ接続しました');}
  catch(e){await removeSecureToken();showSetup();notify('接続できません: '+e.message);}
});

$('refreshButton').addEventListener('click',()=>refresh().catch(e=>notify(e.message)));
$('dueButton').addEventListener('click',async()=>{
  try{
    notify('投稿状態を確認中…');
    await request('/api/mobile/control',{method:'POST',body:{action:'due'}});
    await refresh();
    notify('投稿判定を実行しました');
  }catch(e){notify(e.message)}
});

$('autopilotButton').addEventListener('click',async()=>{
  const armed=Boolean(state&&state.autopilot&&state.autopilot.armed);
  const action=armed?'stop':'start';
  if(!confirm(armed?'完全自動運用を停止しますか？':'完全自動運用を開始しますか？')) return;
  try{
    await request('/api/mobile/control',{method:'POST',body:{action}});
    await refresh();
    notify(action==='start'?'完全自動運用を開始しました':'停止しました');
  }catch(e){notify(e.message)}
});

$('disconnectButton').addEventListener('click',async()=>{
  if(!confirm('この端末の接続設定を削除しますか？')) return;
  localStorage.removeItem(KEY_URL);
  await removeSecureToken();
  state=null;
  showSetup();
});

document.addEventListener('visibilitychange',()=>{
  if(!document.hidden&&connection().token) refresh().catch(()=>{});
});

async function boot(){
  await loadSecureToken();
  const cfg=connection();
  if(!cfg.url||!cfg.token){showSetup();return;}
  showDashboard();
  try{
    await refresh();
    timer=setInterval(()=>{if(!document.hidden) refresh().catch(()=>{});},30000);
  }catch(e){notify('再接続が必要です: '+e.message);showSetup();}
}
if(!Capacitor.isNativePlatform()){
  console.warn('[MIRAI] Web preview uses browser storage; production tokens are protected only in native Android/iOS builds.');
}
boot();

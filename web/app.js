'use strict';
const $ = id => document.getElementById(id);
let csrf = '', state = null, selectedJob = '', pending = null, polling = false, uploading = false;
let messageTimer, backupKey = '';
function message(text) { $('message').textContent = text; $('message').hidden = false; clearTimeout(messageTimer); messageTimer = setTimeout(() => { $('message').hidden = true; }, 14000); }
function bytes(n) { if(n===null)return '目录'; return (n / 1024 / 1024).toFixed(1) + ' MB'; }
function stamp(value) { return new Date(value).toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'}); }
function showLogin() { $('workspace').hidden = true; $('loginPanel').hidden = false; $('logout').hidden = true; csrf = ''; }
async function api(path, body) {
  const response = await fetch('api/' + path, { method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf}, body: body === undefined ? undefined : JSON.stringify(body), credentials:'same-origin' });
  let data;
  try { data = await response.json(); } catch (_) { throw new Error('服务器未返回有效数据，请检查连接或 Nginx 配置。'); }
  if (!response.ok) { if (response.status === 401 && path !== 'login') showLogin(); throw new Error(data.error || '请求失败'); }
  return data;
}
function fillSelect(el, items, placeholder, key, label) {
  const previous = el.value;
  const next = items.map(item => ({value:key(item), text:label(item)}));
  if (!next.length) next.push({value:'', text:placeholder});
  if (JSON.stringify(Array.from(el.options, o => ({value:o.value, text:o.text}))) !== JSON.stringify(next)) {
    el.replaceChildren(...next.map(item => new Option(item.text, item.value)));
    if (next.some(item => item.value === previous)) el.value = previous;
  }
}
function patchInfo() {
  const patch = state && state.uploads.find(p => p.id === $('patches').value);
  $('patchInfo').replaceChildren();
  if (patch) {
    const info = document.createElement('span'); info.textContent = bytes(patch.size) + ' · ' + stamp(patch.created);
    const sha = document.createElement('code'); sha.textContent = 'SHA-256 ' + patch.sha256;
    $('patchInfo').append(info, sha);
  }
  $('deploy').disabled = !patch || !!(state && state.status.active);
  $('removeUpload').disabled = !patch || !!(state && state.status.active);
}
const labels = {running:'正在执行',succeeded:'检查通过',failed:'执行失败',interrupted:'监控中断，需核实'};
function render() {
  const s = state.status, front = s.deployment_type === 'frontend';
  const running = front ? s.frontend_ready : s.pids.length > 0 && s.port_open;
  $('pageTitle').textContent=front?'前端补丁':'后端补丁';
  $('modeLabel').textContent=front?'前端补丁更新':'后端补丁更新';
  $('fileType').textContent=front?'ZIP 文件，含 dist/':'JAR 文件';
  $('file').accept=front?'.zip':'.jar';
  if(!$('file').files.length)$('filename').textContent=front?'选择本地前端补丁 ZIP':'选择本地后端补丁';
  $('updateNote').textContent=front?'更新会备份当前 dist 并切换新目录，可能有短暂访问间隙。不会重启 Nginx 或后端。':'更新会暂时停止影像后端。当前正式包会先备份，更新失败后可选择备份回退。';
  $('footerNote').textContent=front?'检查通过表示静态文件切换与校验完成。请强制刷新页面，验证登录及补丁功能。':'检查通过表示 Java 进程与后端端口稳定。更新后请访问影像功能，确认业务可用。';
  $('logSummary').textContent=front?'查看附加日志':'查看启停脚本输出';
  $('target').textContent = s.host + '　/　' + s.app_dir + (front?'':'　/　端口 ' + s.port);
  $('serviceState').textContent = s.active ? '更新进行中' : front ? (running?'前端入口存在':'前端目录需检查') : running ? '进程与端口正常' : s.pids.length ? '进程存在，端口未就绪' : '未检测到目标进程';
  $('serviceState').className = 'status ' + (s.active ? '' : running ? 'good' : 'bad');
  $('upload').disabled = !$('file').files.length || uploading;
  $('filehint').textContent = '最大 ' + bytes(s.max_upload) + '；校验通过后再执行更新';
  fillSelect($('patches'), state.uploads, '暂无补丁，请先上传', p=>p.id, p=>p.name + '（' + stamp(p.created) + '）');
  patchInfo();
  fillSelect($('jobs'), state.jobs, '暂无执行记录', j=>j.id, j=>stamp(j.created) + ' ' + (j.kind==='update'?'更新':'回退') + ' · ' + labels[j.state]);
  if (s.active) selectedJob = s.active;
  if (!selectedJob && state.jobs.length) selectedJob = state.jobs[0].id;
  if (selectedJob) $('jobs').value = selectedJob;
  const job = state.jobs.find(j=>j.id === selectedJob);
  if (job) {
    $('jobState').textContent = labels[job.state];
    $('jobSource').textContent = (job.kind==='update'?'更新':'回退') + ' · ' + stamp(job.created);
    $('events').replaceChildren(...job.events.map(event=>{
      const li = document.createElement('li'), time = document.createElement('time'), text = document.createElement('span');
      time.textContent=stamp(event.time);text.textContent=event.message;li.append(time,text);return li;
    }));
  }
  const nextBackupKey=JSON.stringify([state.backups,!!s.active]);
  if(nextBackupKey!==backupKey) {
    backupKey=nextBackupKey;
    $('backups').replaceChildren();
    if (!state.backups.length) { const row=$('backups').insertRow();const cell=row.insertCell();cell.colSpan=3;cell.textContent='暂无备份，首次更新时会自动创建。'; }
    for (const backup of state.backups) {
      const row=$('backups').insertRow(); row.insertCell().textContent=backup.id;row.insertCell().textContent=bytes(backup.size);
      const button=document.createElement('button');button.className='secondary';button.textContent='回退';button.disabled=!!s.active;
      button.addEventListener('click',()=>confirmAction('rollback',backup.id,backup.id));row.insertCell().append(button);
    }
  }
}
async function refresh(silent=false) {
  if (polling) return;
  polling=true;
  try {
    state=await api('state');csrf=state.csrf;
    $('loginPanel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('connection').hidden=true;
    render();
    if(selectedJob) { const log=await api('log/'+encodeURIComponent(selectedJob));$('scriptLog').textContent=log.text || '暂无脚本输出'; }
  } catch(error) {
    if(csrf) { $('connection').hidden=false;$('connection').textContent='连接暂时中断，正在重试。请勿重复提交更新。'; }
    if(!silent) message(error.message);
  } finally { polling=false; }
}
function confirmAction(kind, source, name) {
  $('confirmNote').textContent=state.status.deployment_type==='frontend'?'此操作会切换前端目录，可能有短暂访问间隙。不会重启 Nginx 或后端。':'此操作会停止并重启影像后端，请安排好更新时间。';
  $('confirmTarget').textContent=state.status.host + ' / ' + state.status.app_dir;
  pending={kind,source};$('confirmTitle').textContent=kind==='update'?'确认更新补丁':'确认回退版本';$('confirmSource').textContent=name;
  $('confirmInput').value='';$('confirmDialog').showModal();$('confirmInput').focus();
}
$('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter || $('loginForm').querySelector('button');button.disabled=true;
  try { const data=await api('login',{password:$('password').value});csrf=data.csrf;$('password').value='';await refresh(); }catch(error){message(error.message);}finally{button.disabled=false;}
});
$('logout').addEventListener('click',async()=>{try{await api('logout',{});showLogin();}catch(error){message(error.message);}});
$('file').addEventListener('change',()=>{const file=$('file').files[0];$('filename').textContent=file?file.name:(state && state.status.deployment_type==='frontend'?'选择本地前端补丁 ZIP':'选择本地后端补丁');$('upload').disabled=!file || uploading;});
$('upload').addEventListener('click',()=>{
  const file=$('file').files[0];if(!file)return;
  const suffix=state && state.status.deployment_type==='frontend'?'.zip':'.jar';
  if(!file.name.toLowerCase().endsWith(suffix))return message('请选择 '+suffix+' 补丁文件。');
  if(state && file.size>state.status.max_upload)return message('文件超过上传大小限制。');
  uploading=true;$('upload').disabled=true;$('progress').hidden=false;$('progress').value=0;
  const xhr=new XMLHttpRequest();xhr.open('POST','api/upload');xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.setRequestHeader('X-CSRF-Token',csrf);
  const encoded=Array.from(new TextEncoder().encode(file.name),c=>String.fromCharCode(c)).join('');xhr.setRequestHeader('X-File-Name',btoa(encoded));
  xhr.upload.onprogress=event=>{if(event.lengthComputable){$('progress').value=event.loaded/event.total*100;$('uploadState').textContent=event.loaded===event.total?'上传完成，服务器正在校验…':'正在上传 '+Math.floor(event.loaded/event.total*100)+'%';}};
  xhr.onload=async()=>{uploading=false;$('upload').disabled=false;try{const data=JSON.parse(xhr.responseText);if(xhr.status!==201)throw new Error(data.error || '上传失败');$('uploadState').textContent='完整性校验通过。';await refresh();$('patches').value=data.id;patchInfo();}catch(error){$('uploadState').textContent='上传未完成。';message(error.message);}};
  xhr.onerror=()=>{uploading=false;$('upload').disabled=false;$('uploadState').textContent='连接中断，请刷新查看补丁列表。';message('上传连接中断。');};xhr.send(file);
});
$('patches').addEventListener('change',patchInfo);
$('deploy').addEventListener('click',()=>{const patch=state.uploads.find(p=>p.id===$('patches').value);if(patch)confirmAction('update',patch.id,patch.name);});
$('removeUpload').addEventListener('click',async()=>{const source=$('patches').value;if(!source || !window.confirm('删除选中的上传文件？不会删除正式包和备份。'))return;try{await api('remove-upload',{source});await refresh();}catch(error){message(error.message);}});
$('cancel').addEventListener('click',()=>$('confirmDialog').close());
$('confirmForm').addEventListener('submit',async event=>{
  event.preventDefault();if($('confirmInput').value!=='更新影像')return message('请输入“更新影像”。');
  const button=$('confirmForm').querySelector('[type=submit]');button.disabled=true;
  try { const result=await api('deploy',{...pending,confirm:'更新影像'});selectedJob=result.id;$('confirmDialog').close();await refresh(); }catch(error){$('confirmDialog').close();message(error.message+'。若连接中断，请先刷新检查执行记录。');}finally{button.disabled=false;}
});
$('jobs').addEventListener('change',()=>{selectedJob=$('jobs').value;refresh();});
refresh(true);setInterval(()=>{if(csrf)refresh(true);},2500);

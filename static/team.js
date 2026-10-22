"use strict";

const audioLabels={queued:'等待转写',running:'转写中',transcribed:'待校对',confirmed:'已确认',failed:'转写失败',interrupted:'转写中断'};
const audioColors={queued:'gray',running:'blue',transcribed:'amber',confirmed:'green',failed:'red',interrupted:'orange'};
function customerName(cid){return S.customers.find(c=>c.id===cid)?.name||'客户档案';}
function reviewBadge(row){const d=row.review_display;return d?pill(d.label,d.color):pill('待处理','amber');}
function ownerOptions(selected='',includePool=true){return `${includePool?`<option value="" ${!selected?'selected':''}>待分配客户池</option>`:''}${S.users.filter(u=>u.active).map(u=>`<option value="${u.id}" ${selected===u.id?'selected':''}>${esc(u.name)} · ${esc(u.id)}${u.role==='admin'?'（管理员）':''}</option>`).join('')}`;}
function customerOptions(selected='',query=''){return `<option value="">请选择归属客户</option>${S.customers.filter(c=>!query||[c.name,c.contact,c.phone].join(' ').toLowerCase().includes(query.toLowerCase())).map(c=>`<option value="${c.id}" ${selected===c.id?'selected':''}>${esc([c.name,c.contact,c.phone].filter(Boolean).join(' · '))}${S.user.role==='admin'?` · ${esc(c.owner_name||'待分配')}`:''}</option>`).join('')}`;}

function renderLogin(){
  S.epoch++;S.modelConfig=null;S.audioCustomerFilter=null;S.ownerFilter='all';S.customerQuery='';S.batchPreview=null;
  clearTimeout(S.poll);clearTimeout(S.audioPoll);S.poll=null;S.audioPoll=null;
  S.user=null;S.csrf='';S.rows=[];S.customers=[];S.users=[];S.current=null;S.draft=null;S.audioCurrent=null;S.audioDraft='';S.audioDraftDirty=false;S.audioPendingSnapshot=null;S.audioRows=[];S.selected.clear();S.customerSelection.clear();S.config={};S.metrics={};S.view='workspace';
  closeModal();document.body.classList.add('logged-out');
  main.innerHTML=`<div class="login-layout"><section class="login-story"><img src="/static/kujiale-enterprise.svg" alt="酷家乐企业版" width="188"><div class="login-message"><div class="eyebrow">KUJIALE · XDR WORKSPACE</div><h1>每一次对话，<br>都成为有据可循的商机。</h1><p>录音转写 · 客户资源 · 线索审核<br>让销售团队专注于真正值得跟进的需求。</p><div class="login-flow"><span>听见需求</span><span>核实事实</span><span>协同跟进</span></div></div><small>AI SDR 工作台 · 作业演示</small></section><section class="login-side"><form id="login-form" class="login-card"><span class="login-icon">${icon('shield')}</span><h2>登录工作空间</h2><p class="subtitle">使用你的员工账号，继续客户跟进。</p><div class="field"><label class="field-label" for="login-username">登录账号</label><input id="login-username" name="username" autocomplete="username" required maxlength="40" placeholder="请输入账号" autocapitalize="none" spellcheck="false"></div><div class="field"><label class="field-label" for="login-password">密码</label><input id="login-password" name="password" type="password" autocomplete="current-password" required maxlength="128" placeholder="请输入密码"></div><div id="login-error" role="alert" class="form-error" hidden></div><button class="btn primary login-submit" type="submit">登录 ${icon('arrow')}</button><p class="small-note">账号由管理员统一创建；忘记密码请联系管理员。</p></form></section></div>`;
}
async function loginSubmit(){
  const button=$('#login-form button');button.disabled=true;$('#login-error').hidden=true;
  try{const session=await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('#login-username').value.trim().toLowerCase(),password:$('#login-password').value})});S.user=session.user;S.csrf=session.csrf;document.body.classList.remove('logged-out');await initialize();}
  catch(e){if($('#login-error')){$('#login-error').textContent=e.message;$('#login-error').hidden=false;}}
  finally{if(button.isConnected)button.disabled=false;}
}
function accountDialog(){
  showModal(`<div class="modal-head"><h2>账号与安全</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><p>${esc(S.user.name)} · ${esc(S.user.id)} ${pill(S.user.role==='admin'?'管理员':'员工','blue')}</p><div class="field"><label class="field-label" for="current-password">原密码</label><input id="current-password" type="password" autocomplete="current-password"></div><div class="field"><label class="field-label" for="new-password">新密码（至少12个字符）</label><input id="new-password" type="password" minlength="12" maxlength="128" autocomplete="new-password"></div><p class="small-note">修改密码后，所有设备的登录会话都会退出。</p></div><div class="modal-actions"><button class="btn secondary" data-team="logout">退出登录</button><button class="btn primary" data-team="change-password">修改密码</button></div>`);
}

function customersView(){
  const admin=S.user.role==='admin';
  const rows=S.customers.filter(c=>(S.ownerFilter==='all'||(S.ownerFilter==='unassigned'?!c.owner_id:c.owner_id===S.ownerFilter))&&(!S.customerQuery||[c.name,c.contact,c.phone,c.owner_name].join(' ').toLowerCase().includes(S.customerQuery.toLowerCase())));
  const owners=[...new Map(S.customers.filter(c=>c.owner_id).map(c=>[c.owner_id,c.owner_name||c.owner_id])).entries()];
  main.innerHTML=heading(admin?'让客户资源，找到合适的负责人。':'我的客户，每一次跟进都有记录。',admin?'统一查看客户归属与通话记录。转交客户时，相关录音、分析和审核历史一并转交。':'仅显示你负责的客户。录音、通话分析与审核结果统一归入客户档案。','CUSTOMER RESOURCES',`<button class="btn primary" data-team="new-customer">${icon('plus')} 新建客户</button>`)+`
  <div class="resource-overview"><div><strong>${S.customers.length}</strong><span>${admin?'全部客户':'我的客户'}</span></div><div><strong>${S.customers.reduce((n,c)=>n+c.call_count,0)}</strong><span>通话分析</span></div><div><strong>${S.customers.reduce((n,c)=>n+c.audio_count,0)}</strong><span>录音档案</span></div>${admin?`<div><strong>${S.customers.filter(c=>!c.owner_id).length}</strong><span>待分配客户</span></div>`:''}</div>
  <div class="toolbar"><div class="filter-controls">${admin?`<label class="sr-only" for="owner-filter">按负责人筛选</label><select id="owner-filter"><option value="all">全部负责人</option><option value="unassigned" ${S.ownerFilter==='unassigned'?'selected':''}>待分配客户池</option>${owners.map(([id,name])=>`<option value="${id}" ${S.ownerFilter===id?'selected':''}>${esc(name)} · ${S.customers.filter(c=>c.owner_id===id).length} 位客户</option>`).join('')}</select>`:pill('仅本人负责','blue')}</div><input class="search" id="customer-search" aria-label="搜索客户" placeholder="搜索客户、联系人或负责人" value="${esc(S.customerQuery)}"></div>
  ${admin&&S.customerSelection.size?`<div class="batch-bar">已选择 ${S.customerSelection.size} 位客户<span class="small-note">整批转交，保留历史操作记录</span><button class="btn primary" data-team="assign-customers">分配 / 转交客户</button></div>`:''}
  <section class="card">${rows.length?`<div class="table-wrap"><table><thead><tr>${admin?`<th><input type="checkbox" class="checkbox" id="all-customers" aria-label="选择全部显示的客户" ${rows.every(c=>S.customerSelection.has(c.id))?'checked':''}></th>`:''}<th>客户档案</th><th>联系人</th><th>负责人</th><th>通话 / 录音</th><th>更新时间</th><th></th></tr></thead><tbody>${rows.map(c=>`<tr>${admin?`<td><input type="checkbox" class="checkbox customer-checkbox" data-id="${c.id}" aria-label="选择客户${esc(c.name)}" ${S.customerSelection.has(c.id)?'checked':''}></td>`:''}<td><button class="title-link" data-team="customer-detail" data-id="${c.id}">${esc(c.name)}</button><small>${esc(c.notes||c.id)}</small></td><td>${esc(c.contact||'—')}<small>${esc(c.phone||'—')}</small></td><td>${pill(c.owner_name||'待分配',c.owner_id?'blue':'orange')}<small>${esc(c.owner_id||'管理员可分配')}</small></td><td>${c.call_count} 次分析<small>${c.audio_count} 份录音</small></td><td>${dateText(c.updated_at)}</td><td><div class="row-actions"><button class="text-button" data-team="customer-detail" data-id="${c.id}">查看档案</button><button class="text-button" data-team="edit-customer" data-id="${c.id}">编辑</button><button class="text-button" data-team="delete-customer" data-id="${c.id}">删除</button>${admin?`<button class="text-button" data-team="assign-one" data-id="${c.id}">转交 →</button>`:''}</div></td></tr>`).join('')}</tbody></table></div><div class="queue-footer">${rows.length} 位客户 · 客户名称仅用于管理，不代替企业主体核验</div>`:empty('还没有客户档案','先创建客户，再上传录音或录入通话进行分析。')}</section>`;
  S.visibleCustomerIds=rows.map(c=>c.id);
}
function editCustomerDialog(cid){
  const c=S.customers.find(x=>x.id===cid);if(!c)return;
  showModal(`<div class="modal-head"><h2>编辑客户档案</h2><button class="icon-button" data-action="close-modal">${icon('close')}</button></div><div class="modal-body"><div class="field"><label class="field-label" for="edit-customer-name">客户名称</label><input id="edit-customer-name" maxlength="100" value="${esc(c.name)}"></div><div class="field"><label class="field-label" for="edit-customer-contact">联系人（选填）</label><input id="edit-customer-contact" maxlength="100" value="${esc(c.contact)}"></div><div class="field"><label class="field-label" for="edit-customer-phone">联系电话（选填）</label><input id="edit-customer-phone" maxlength="50" value="${esc(c.phone)}"></div><div class="field"><label class="field-label" for="edit-customer-notes">跟进备注（选填）</label><textarea id="edit-customer-notes" maxlength="2000">${esc(c.notes)}</textarea></div><p class="small-note">更新客户档案，不会改写历史通话、企业核验结果或客户负责人。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="update-customer" data-id="${c.id}" data-revision="${c.revision}">保存修改</button></div>`);
}
function deleteCustomerDialog(cid){
  const c=S.customers.find(x=>x.id===cid);if(!c)return;
  const linked=c.call_count||c.audio_count;
  showModal(`<div class="modal-head"><h2>删除客户：${esc(c.name)}</h2></div><div class="modal-body"><p>${linked?`该客户有 ${c.call_count} 条分析、${c.audio_count} 份录音，请先处理关联记录后再删除客户。`:'确认删除此客户档案？操作记录会保留。'}</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button>${linked?'':`<button class="btn danger" data-team="confirm-delete-customer" data-id="${c.id}" data-revision="${c.revision}">确认删除</button>`}</div>`);
}
async function newCustomerDialog(){
  if(S.user.role==='admin')S.users=await api('/api/admin/users');
  showModal(`<div class="modal-head"><h2>新建客户档案</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div class="field"><label class="field-label" for="customer-name">客户名称</label><input id="customer-name" maxlength="100" placeholder="企业或门店的常用名称"></div><div class="field-grid"><div class="field"><label class="field-label" for="customer-contact">联系人（选填）</label><input id="customer-contact" maxlength="100"></div><div class="field"><label class="field-label" for="customer-phone">联系电话（选填）</label><input id="customer-phone" maxlength="50"></div></div>${S.user.role==='admin'?`<div class="field"><label class="field-label" for="customer-owner">负责人</label><select id="customer-owner">${ownerOptions()}</select></div>`:''}<div class="field"><label class="field-label" for="customer-notes">跟进备注（选填）</label><textarea id="customer-notes" rows="3" maxlength="2000"></textarea></div><p class="small-note">客户档案与主体核验结果分开保存，不自动用档案信息填充原文事实。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="save-customer">创建客户</button></div>`);
}
async function assignmentDialog(ids){
  S.users=await api('/api/admin/users');
  S.assignmentItems=S.customers.filter(c=>ids.includes(c.id)).map(c=>({id:c.id,revision:c.revision}));
  if(!S.assignmentItems.length)return;
  showModal(`<div class="modal-head"><h2>调配 ${S.assignmentItems.length} 位客户</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div class="assignment-preview">${S.customers.filter(c=>ids.includes(c.id)).map(c=>`<p><strong>${esc(c.name)}</strong><span>当前：${esc(c.owner_name||'待分配')}</span></p>`).join('')}</div><div class="field"><label class="field-label" for="assignment-owner">转交给</label><select id="assignment-owner">${ownerOptions()}</select></div><div class="field"><label class="field-label" for="assignment-reason">分配 / 转交理由</label><textarea id="assignment-reason" rows="3" maxlength="500" placeholder="例如：调整区域分工，由杭州团队继续跟进"></textarea></div><div class="notice info">相关录音与通话记录一并转交。提交后原负责人将失去访问权限；整批操作遇到归属变化时不会部分提交。</div></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="commit-assignment">确认调配客户</button></div>`);
}
async function customerDetail(cid){
  const c=S.customers.find(x=>x.id===cid);if(!c)return;
  const events=await api(`/api/customers/${cid}/events`);
  const rows=S.rows.filter(r=>r.customer_id===cid);
  showModal(`<div class="modal-head"><h2>${esc(c.name)}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><p>负责人：${esc(c.owner_name||'待分配')} · ${esc(c.contact||'联系人待补充')}</p><p class="small-note">${esc(c.notes||'暂无跟进备注')}</p><h3>客户通话</h3><div class="customer-calls">${rows.length?rows.map(r=>`<button data-team="customer-call" data-id="${r.id}"><span>${esc(r.title)}</span>${reviewBadge(r)}<span>→</span></button>`).join(''):empty('暂无分析','录入该客户的第一段通话。')}</div><h3>资源调配记录</h3>${events.filter(e=>['customer_assigned','customer_created','customer_updated','legacy_import'].includes(e.action)).map(e=>`<div class="audit-event"><div class="audit-title">${esc(e.actor)} · ${e.action==='customer_assigned'?'调配客户':e.action==='legacy_import'?'历史档案迁移':e.action==='customer_updated'?'编辑客户':'创建客户'}</div><div class="audit-meta">${dateText(e.created_at)}</div><div class="audit-reason">${esc(e.reason)}</div>${e.action==='customer_assigned'?`<div class="audit-diff">${esc(e.before_value?.owner_id||'待分配')} → ${esc(e.after_value?.owner_id||'待分配')}</div>`:''}</div>`).join('')}</div><div class="modal-actions"><button class="btn secondary" data-team="customer-audio" data-id="${cid}">查看录音</button><button class="btn primary" data-team="customer-analyze" data-id="${cid}">新增通话分析</button></div>`);
}

async function usersView(){
  if(S.user.role!=='admin')return;
  S.users=await api('/api/admin/users');if(S.view!=='users')return;
  main.innerHTML=heading('每位员工，一个独立工作空间。','创建员工账号、停用离职账号或重置密码。客户归属通过客户资源页统一调配。','TEAM & ACCESS',`<button class="btn primary" data-team="new-user">${icon('plus')} 创建账号</button>`)+`<section class="card"><div class="table-wrap"><table><thead><tr><th>员工</th><th>角色</th><th>账号状态</th><th>负责客户</th><th>创建时间</th><th></th></tr></thead><tbody>${S.users.map(u=>`<tr><td><strong>${esc(u.name)}</strong><small>${esc(u.id)}</small></td><td>${pill(u.role==='admin'?'管理员':'员工',u.role==='admin'?'purple':'blue')}</td><td>${pill(u.active?'启用':'已停用',u.active?'green':'gray')}</td><td>${S.customers.filter(c=>c.owner_id===u.id).length}</td><td>${dateText(u.created_at)}</td><td><div class="row-actions"><button class="text-button" data-team="reset-user" data-id="${u.id}">重置密码</button>${u.id!==S.user.id?`<button class="text-button" data-team="toggle-user" data-id="${u.id}">${u.active?'停用':'启用'}</button>`:''}</div></td></tr>`).join('')}</tbody></table></div><div class="queue-footer">停用或重置密码会撤销该账号全部登录会话；客户历史记录继续保留。</div></section>`;
}
function newUserDialog(){showModal(`<div class="modal-head"><h2>创建团队账号</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div class="field"><label class="field-label" for="user-name">员工姓名</label><input id="user-name" maxlength="50"></div><div class="field"><label class="field-label" for="user-username">登录账号</label><input id="user-username" autocomplete="off" maxlength="40" placeholder="小写字母、数字、点、下划线或短横线"></div><div class="field"><label class="field-label" for="user-password">初始密码（至少12个字符）</label><input id="user-password" type="password" autocomplete="new-password" minlength="12" maxlength="128"></div><div class="field"><label class="field-label" for="user-role">角色</label><select id="user-role"><option value="employee">员工 · 仅负责自己的客户</option><option value="admin">管理员 · 全部客户和账号管理</option></select></div></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="save-user">创建账号</button></div>`);}
function updateUserDialog(uid,reset){const u=S.users.find(x=>x.id===uid);S.userTarget=u;showModal(`<div class="modal-head"><h2>${reset?'重置密码':u.active?'停用账号':'启用账号'} · ${esc(u.name)}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body">${reset?`<div class="field"><label class="field-label" for="reset-password">新密码（至少12个字符）</label><input id="reset-password" type="password" autocomplete="new-password" minlength="12" maxlength="128"></div>`:''}<div class="field"><label class="field-label" for="user-reason">操作原因</label><textarea id="user-reason" rows="3" maxlength="500"></textarea></div><p class="small-note">${reset||u.active?'该账号已登录的设备会退出。':'启用后可以重新登录。'}客户资源可在客户资源页另行调配。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="update-user" data-reset="${reset}">确认${reset?'重置':u.active?'停用':'启用'}</button></div>`);}

function audioTime(value){const seconds=Math.max(0,Math.round(value));return `${Math.floor(seconds/60).toString().padStart(2,'0')}:${(seconds%60).toString().padStart(2,'0')}`;}
function audioSegmentsPanel(a){
  if(!a.segments?.length)return '';
  const completed=a.segments.filter(s=>s.status==='completed').length;
  return `<section class="card audio-segments"><div class="card-head"><div><h2>分段转写进度</h2><p>已完成 ${completed}/${a.segments.length} 段 · 按录音顺序合并</p></div>${pill(completed===a.segments.length?'片段已完成':'保留已完成结果',completed===a.segments.length?'green':'blue')}</div><div class="card-body"><progress aria-label="录音分段完成进度" max="${a.segments.length}" value="${completed}"></progress><p class="small-note">${a.status==='failed'||a.status==='interrupted'?'已保存的成功片段将在重试时复用。':''}点击片段可查看文字并回放对应位置；最终以完整校对稿为准。</p><div class="audio-segment-list">${a.segments.map(s=>`<details><summary><span>第 ${s.index} 段 · ${audioTime(s.start)} — ${audioTime(s.end)}</span>${pill(s.status==='completed'?'已完成':'待处理',s.status==='completed'?'green':'gray')}</summary><p class="raw-transcript">${esc(s.status==='completed'?(s.text||'此段为数字静音，无转写文字。'):'此片段尚未完成转写。')}</p><button class="text-button" data-team="seek-audio" data-start="${s.start}">从 ${audioTime(s.start)} 回放</button></details>`).join('')}</div></div></section>`;
}
function audioPendingCount(a){return (a?.confirmed_text||'').split('\n').filter(line=>/^\s*待确认\s*[:：]/.test(line)).length;}
function audioBusy(a){return ['queued','running'].includes(a.status)||['queued','running'].includes(a.identity_status)||['queued','running'].includes(a.role_status);}
function audioBadge(a){
  if(['queued','running','failed','interrupted'].includes(a.status))return pill(audioLabels[a.status],audioColors[a.status]);
  if(['queued','running'].includes(a.role_status))return pill('正在分角色','blue');
  if(['queued','running'].includes(a.identity_status))return pill('识别客户中','blue');
  if(a.auto_analyze){
    if(a.auto_analysis_status==='queued'||a.auto_analysis_status==='running')return pill('自动分析中','blue');
    if(a.auto_analysis_status==='manual_ready')return pill('已手动确认 · 可重分析','blue');
    if(a.auto_analysis_status==='submitted'){const pending=audioPendingCount(a);return pending?pill(`已自动分析 · ${pending}处待确认`,'amber'):pill('已提交分析','green');}
    if(a.auto_analysis_status==='failed')return pill('自动分析失败','red');
    if(a.auto_analysis_status==='interrupted')return pill('自动分析中断','amber');
  }
  if(a.status==='confirmed'&&audioPendingCount(a))return pill(`${audioPendingCount(a)}处角色待确认`,'amber');
  if(!a.customer_id)return pill('待确认客户','purple');
  return pill(audioLabels[a.status],audioColors[a.status]);
}
function audioItems(rows){return rows.map(a=>`<button class="audio-item" data-team="open-audio" data-id="${a.id}"><span class="audio-item-icon">${icon('audio')}</span><span class="audio-item-copy"><strong>${esc(a.filename)}</strong><small>${esc(a.customer_name||'客户身份待确认')} · ${audioTime(a.duration)} · ${dateText(a.created_at)}${a.provider.segment_count?` · 已完成 ${a.provider.completed_segments||0}/${a.provider.segment_count} 段`:""}</small></span>${audioBadge(a)}<span>→</span></button>`).join('');}
async function audioView(){
  clearTimeout(S.audioPoll);S.audioRows=await api('/api/transcriptions');if(S.view!=='audio')return;
  const rows=S.audioCustomerFilter?S.audioRows.filter(a=>a.customer_id===S.audioCustomerFilter):S.audioRows;
  main.innerHTML=heading('先听懂通话，再确认客户。','选择已有客户直接归档，或让 AI 从录音中提取身份线索，确认后关联客户。','CALL TRANSCRIPTION',pill(S.config.asr_available?'语音服务已配置':'语音服务未配置',S.config.asr_available?'green':'orange'))+`
  <div class="audio-layout"><section class="card audio-upload-card"><div class="card-head"><h2>上传通话录音</h2></div><div class="card-body">
    <fieldset class="audio-mode-options"><legend class="field-label">客户识别方式</legend>
      <label class="audio-mode-option"><input type="radio" name="audio-customer-mode" value="ai" ${!S.config.llm_available?'disabled':''}><span><strong>AI 识别客户</strong><small>先转写，提取身份线索并推荐客户，由你确认归属。</small></span></label>
      <label class="audio-mode-option"><input type="radio" name="audio-customer-mode" value="existing" checked><span><strong>选择已有客户</strong><small>从已创建的客户档案中选择，录音直接归入该客户。</small></span></label>
    </fieldset>
    ${!S.config.llm_available?'<p class="small-note">客户识别模型尚未配置，可先选择已有客户。</p>':''}
    <div id="audio-existing-fields" class="field"><div class="label-row"><label class="field-label" for="audio-customer">归属客户</label><button class="text-button" data-team="new-customer">新建客户 +</button></div><input id="audio-customer-search" type="search" aria-label="搜索已有客户" placeholder="搜索客户名称、联系人或电话"><select id="audio-customer">${customerOptions(S.audioCustomerFilter||'')}</select></div>
    <div id="audio-ai-note" class="notice info" hidden>无需预先建档。AI 根据录音中的公司、联系人、电话提供建议；信息不足时可以手动选择或新建客户。</div>
    <div class="field"><label class="field-label" for="audio-data-mode">数据类型</label><select id="audio-data-mode"><option value="real">真实业务录音</option><option value="synthetic">合成演示录音</option><option value="snapshot_demo">题目 / 脱敏录音</option></select></div>
    <label class="audio-drop" for="audio-file">${icon('audio')}<strong>选择通话录音</strong><span>WAV · MP3 · M4A · FLAC · OGG</span><span>单个文件 ≤${S.config.audio_max_bytes/1024/1024} MB，时长 ≤${S.config.audio_max_seconds/60}分钟</span><input id="audio-file" type="file" accept=".wav,.mp3,.m4a,.flac,.ogg" multiple aria-label="选择通话录音"></label>
    <p class="small-note">转写后自动整理销售／客户逐轮对话；长录音分段合并，中断后可继续。批量上传：选择已有客户时归入该客户，否则按文件名自动建档；电话、联系人留空，人工审核时再补。</p><p id="audio-file-name" class="small-note">尚未选择文件</p><label class="consent-line"><input id="audio-consent" type="checkbox" class="checkbox"><span id="audio-consent-copy">将录音提交到已配置的语音服务转写，并将转写内容提交到文字分析模型划分销售和客户角色。</span></label><label class="consent-line"><input id="audio-auto-analyze" type="checkbox" class="checkbox" checked><span>上传后自动确认角色并提交分析（使用所选客户；电话、联系人可后补）</span></label><div id="audio-upload-error" class="form-error" hidden role="alert"></div><div class="audio-submit-row"><button class="btn primary audio-submit" data-team="upload-audio" ${!S.config.asr_available?'disabled':''}>${icon('upload')} 上传并转写</button><button class="btn secondary audio-submit" data-team="batch-upload-audio" ${!S.config.asr_available?'disabled':''}>${icon('upload')} 批量上传并自动分析</button></div>
  </div></section><section class="card"><div class="card-head"><div><h2>录音档案</h2><p>原始录音、机器转写与人工校对稿分别保存</p></div><button class="text-button" data-team="refresh-audio">刷新</button></div>${S.audioCustomerFilter?`<div class="audio-filter">${esc(customerName(S.audioCustomerFilter))}<button class="text-button" data-team="clear-audio-filter">显示全部</button></div>`:''}${rows.length?`<div class="audio-list">${audioItems(rows)}</div>`:empty('还没有录音','选择客户识别方式，上传第一段通话录音。')}</section></div>`;
  scheduleAudioListPoll();
}
function scheduleAudioListPoll(){
  if(S.view==='audio'&&S.audioRows.some(audioBusy))S.audioPoll=setTimeout(async()=>{try{
    const rows=await api('/api/transcriptions');if(S.view!=='audio')return;S.audioRows=rows;
    const list=$('.audio-list');if(list)list.innerHTML=audioItems(rows.filter(a=>!S.audioCustomerFilter||a.customer_id===S.audioCustomerFilter));
    scheduleAudioListPoll();
  }catch(e){toast(e.message,true);}},1800);
}
async function openAudio(tid){const draft=S.audioCurrent?.id===tid&&S.audioDraftDirty?S.audioDraft:null;clearTimeout(S.audioPoll);clearTimeout(S.poll);if(S.view==='workspace')saveDraft();await loadOverview();S.audioCurrent=await api(`/api/transcriptions/${tid}`);if(!draft&&S.audioCurrent.customer_id&&S.audioCurrent.role_status==='ready'&&['transcribed','confirmed'].includes(S.audioCurrent.status)&&!S.audioCurrent.auto_analysis_id&&!audioPendingCount(S.audioCurrent)&&!['failed','interrupted'].includes(S.audioCurrent.auto_analysis_status)){try{S.audioCurrent=await api(`/api/transcriptions/${tid}/auto-analyze`,{method:'POST'});}catch(e){}}S.audioDraft=draft??audioDraftFor(S.audioCurrent);S.audioDraftDirty=draft!==null;S.view='audioDetail';updateChrome();renderAudioDetail();window.scrollTo({top:0});pollAudio(tid);}
function pollAudio(tid){
  clearTimeout(S.audioPoll);if(!audioBusy(S.audioCurrent))return;
  S.audioPoll=setTimeout(async()=>{try{
    const a=await api(`/api/transcriptions/${tid}`);if(S.view!=='audioDetail'||S.audioCurrent.id!==tid)return;
    S.audioCurrent=a;if(!S.audioDraftDirty)S.audioDraft=audioDraftFor(a);renderAudioDetail();pollAudio(tid);
  }catch(e){toast(e.message,true);}},1500);
}
function audioIdentityPanel(a){
  if(a.customer_id)return `<section class="card audio-identity-card"><div class="card-body identity-linked"><div><strong>归属客户已确认</strong><p class="small-note">${esc(customerName(a.customer_id))} · ${a.customer_mode==='ai'?'AI 辅助识别后人工确认':'上传时选择已有客户'}</p></div>${pill('已关联','green')}</div></section>`;
  if(!['transcribed','confirmed'].includes(a.status))return '';
  const result=a.identity_result||{},working=['queued','running'].includes(a.identity_status),clear=result.confidence==='clear';
  const facts=[['company','公司 / 客户名称'],['contact','联系人'],['phone','电话'],['customer_type','业务类型']].filter(([k])=>result[k]);
  const factValue=k=>clear?result[k]?.value||'':'';
  return `<section class="card audio-identity-card"><div class="card-head"><div><h2>确认客户身份</h2><p>根据通话内容识别，核对后归入客户档案</p></div>${pill(working?'AI 正在识别':'等待人工确认',working?'blue':'purple')}</div><div class="card-body">
    ${working?'<div class="notice info"><span class="spinner"></span> 正在从转写中提取客户身份，完成后会显示原文依据和匹配建议。</div>':`
    ${a.identity_error?`<div class="notice error">${esc(a.identity_error)}</div>`:''}
    ${a.identity_status==='outdated'?'<div class="notice info">校对稿已修改，请重新识别，或根据已核对的内容手动选择客户。</div>':''}
    ${a.identity_status==='ready'?`<p class="identity-summary">${esc(result.reason)} ${pill(clear?'有明确身份线索':result.confidence==='uncertain'?'身份存在歧义':'身份信息不足',clear?'blue':'amber')}</p>${facts.length?`<div class="identity-facts">${facts.map(([k,label])=>`<div><span class="field-label">${label}</span><strong>${esc(result[k].value)}</strong><blockquote>“${esc(result[k].quote)}”</blockquote></div>`).join('')}</div>`:''}<p class="small-note">依据：${result.source==='confirmed'?'已保存的校对稿':'机器原始转写'}。请回放核对，名称相近或联系人同名不代表同一客户。</p>`:''}
    <div class="identity-candidates"><h3>可能对应的已有客户</h3>${a.candidates?.length?`<div class="identity-candidate-list">${a.candidates.map(c=>`<button class="identity-candidate" data-team="pick-audio-customer" data-id="${c.id}"><strong>${esc(c.name)}</strong><span>${esc([c.contact,c.phone].filter(Boolean).join(' · ')||'暂无联系人资料')}</span><small>${esc(c.reasons.join(' / '))}</small><b>选择此客户 →</b></button>`).join('')}</div><p class="small-note">共 ${a.candidate_count} 个候选${a.candidate_count>10?'，展示前10个，可在下方搜索更多客户':''}；选中后还需点击确认关联。</p>`:'<p class="small-note">暂未找到可明确匹配的客户。可以从已有档案中选择，或核对身份后新建档案。</p>'}</div>
    <fieldset class="audio-link-options"><legend class="field-label">确认归档方式</legend><label><input type="radio" name="audio-link-mode" value="existing" checked> 关联已有客户</label><label><input type="radio" name="audio-link-mode" value="new"> 新建客户档案</label></fieldset>
    <div id="audio-link-existing"><label class="field-label" for="audio-link-customer">选择已有客户</label><input id="audio-link-search" type="search" aria-label="搜索待关联客户" placeholder="搜索客户名称、联系人或电话"><select id="audio-link-customer">${customerOptions()}</select></div>
    <div id="audio-link-new" hidden><div class="field-grid"><div class="field"><label class="field-label" for="audio-new-name">客户名称 *</label><input id="audio-new-name" maxlength="100" value="${esc(factValue('company'))}" placeholder="核对后填写客户或公司名称"></div><div class="field"><label class="field-label" for="audio-new-contact">联系人</label><input id="audio-new-contact" maxlength="100" value="${esc(factValue('contact'))}"></div><div class="field"><label class="field-label" for="audio-new-phone">电话</label><input id="audio-new-phone" maxlength="50" value="${esc(factValue('phone'))}"></div></div><p class="small-note">确认后创建归你负责的客户档案；管理员可以在客户资源页统一调配。</p></div>
    <div id="audio-identity-error" class="form-error" hidden role="alert"></div><div class="audio-review-actions"><button class="btn primary" data-team="link-audio-customer">确认关联客户</button><button class="btn secondary" data-team="identify-audio" ${!S.config.llm_available?'disabled':''}>重新识别客户</button></div>`}
  </div></section>`;
}
function renderAudioDetail(){
  const a=S.audioCurrent;if(!a)return;const busy=audioBusy(a),transcribing=['queued','running'].includes(a.status),editable=['transcribed','confirmed'].includes(a.status),pending=audioPendingCount(a);
  main.innerHTML=`<button class="back" data-nav="audio">${icon('back')} 返回录音档案</button>`+heading(a.filename,`${a.customer_id?customerName(a.customer_id):'客户身份待确认'} · ${audioTime(a.duration)} · ${(a.size/1024/1024).toFixed(2)} MB`,'TRANSCRIPT REVIEW',audioBadge(a))+`
  <section class="card audio-player"><div><strong>原始通话录音</strong><p class="small-note">边听边核对，尤其注意姓名、企业名称和数字。</p></div><audio controls preload="metadata" src="/api/transcriptions/${a.id}/audio"></audio></section>
  ${a.error?`<div class="notice error audio-notice">${esc(a.error)}</div>`:''}${a.auto_analyze?`<div class="notice ${a.auto_analysis_status==='failed'||a.auto_analysis_status==='interrupted'?'error':pending?'':'info'}">${a.auto_analysis_status==='failed'||a.auto_analysis_status==='interrupted'?`自动分析未完成：${esc(a.auto_analysis_error||'')}<button class="text-button" data-team="retry-auto-analysis">重试自动分析 →</button>`:a.auto_analysis_status==='manual_ready'?'已手动确认校对稿，可点击「直接分析这段录音」重新分析。':pending?`已自动提交分析；${pending} 处角色待确认已标记为不参与事实抽取，修正并保存后可直接分析修正稿。${a.auto_analysis_id?`<button class="text-button" data-action="detail" data-id="${esc(a.auto_analysis_id)}">查看分析结果 →</button>`:''}`:a.auto_analysis_id?`已自动提交分析。<button class="text-button" data-action="detail" data-id="${esc(a.auto_analysis_id)}">查看分析结果 →</button>`:'自动分析排队中…'}${a.auto_role_fallback?' 角色识别未完成，已自动兜底为客户发言，请人工核对。':''}</div>`:''}${transcribing?`<div class="card audio-progress"><span class="spinner"></span><h2>${a.status==='queued'?'录音已保存，正在等待处理':a.provider.stage==='preparing'?'正在预处理录音':a.provider.segment_count?`正在转写第 ${a.provider.current_segment||1}/${a.provider.segment_count} 段`:'正在识别通话内容'}</h2><p class="small-note">${a.customer_mode==='ai'?'转写完成后会继续识别客户身份。':''}可以离开此页，任务会在后台继续处理。</p></div>`:''}
  ${audioSegmentsPanel(a)}${audioIdentityPanel(a)}${audioRolesPanel(a)}
  ${editable?`<div class="audio-review-grid"><section class="card"><div class="card-head"><h2>机器原始转写</h2>${pill('原文保留','gray')}</div><div class="card-body"><p class="raw-transcript">${esc(a.raw_text)}</p><p class="small-note">语音识别服务：${esc(a.provider.model||S.config.asr_model)}。销售／客户的发言角色仍需按录音逐段核对。</p></div></section><section class="card"><div class="card-head"><h2>角色与文字校对</h2>${pill(`修订 v${a.revision}`,'blue')}</div><div class="card-body"><div class="notice info">请先在上方「逐条确认角色」处理待确认语句，再核对这里的完整对话。系统播报可标为「其他：」，无法判断归属可标为「存疑：」并排除分析；确认客户并保存后即可分析。</div><label class="field-label" for="audio-transcript" style="margin-top:18px">校对后的通话转写</label><textarea id="audio-transcript" class="transcript-input" maxlength="${S.config.transcript_max_chars||100000}" ${busy?'disabled':''} placeholder="销售：您好，请问您有哪些需求？&#10;客户：……">${esc(S.audioDraft)}</textarea><div id="audio-confirm-error" class="form-error" hidden role="alert"></div><div id="audio-next-step" class="notice info" role="status"></div><div class="audio-review-actions"><button class="btn secondary" data-team="confirm-audio" ${busy?'disabled':''}>保存校对并确认角色</button>${a.auto_analysis_id&&a.auto_analysis_status==='submitted'?`<button class="btn primary" data-action="detail" data-id="${esc(a.auto_analysis_id)}">查看自动分析结果 ${icon('arrow')}</button>`:`<button class="btn primary" data-team="analyze-audio" ${busy||!a.customer_id||a.status!=='confirmed'||S.audioDraft.trim()!==a.confirmed_text?'disabled':''}>直接分析这段录音 ${icon('arrow')}</button>`}</div></div></section></div>`:''}
  <div class="audio-bottom-actions"><button class="text-button" data-team="refresh-audio-detail">刷新状态</button>${['failed','interrupted'].includes(a.status)?`<button class="btn primary" data-team="retry-audio">${a.provider.segmented?'继续未完成片段':'重新提交转写'}</button>`:''}${!busy?'<button class="text-button" data-team="delete-audio">删除未使用的录音</button>':''}</div>`;
  if(editable)updateAudioReviewControls();
}

async function batchUploadAudio(el,filesArg){
  const files=filesArg||[...($('#audio-file').files||[])];
  if(!files.length)throw new Error('请选择要批量上传的录音文件。');
  if(!$('#audio-consent').checked)throw new Error('请确认将录音提交给语音服务转写。');
  const dataMode=$('#audio-data-mode').value,mode=$('[name="audio-customer-mode"]:checked')?.value||'existing',cid=mode==='existing'?$('#audio-customer').value:'';let ok=0;const failed=[];
  for(let i=0;i<files.length;i++){
    const file=files[i];el.textContent=`正在上传 ${i+1}/${files.length}…`;
    if(file.size>S.config.audio_max_bytes){failed.push(`${file.name}：超过大小限制`);continue;}
    const params=new URLSearchParams({customer_mode:cid?'existing':'auto',...(cid?{customer_id:cid}:{}),auto_analyze:'true',filename:file.name,data_mode:dataMode,idempotency_key:crypto.randomUUID()});
    for(let attempt=0;attempt<5;attempt++){
      try{await api('/api/transcriptions?'+params,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});ok++;break;}
      catch(e){if(attempt===4||!/(较多|稍后|429)/.test(e.message)){failed.push(`${file.name}：${e.message}`);break;}await new Promise(resolve=>setTimeout(resolve,2500));}
    }
  }
  await loadOverview();await audioView();
  toast(`批量任务：已提交 ${ok} 个${failed.length?`，${failed.length} 个失败：${failed.slice(0,2).join('；')}`:''}。`,failed.length>0);
}

async function teamAction(el){
  const action=el.dataset.team;
  if(['roles-audio','apply-audio-roles','accept-audio-roles','assign-audio-role'].includes(action))return audioRoleAction(action,el);
  if(action==='logout'){await api('/api/auth/logout',{method:'POST'});renderLogin();}
  else if(action==='change-password'){await api('/api/auth/password',{method:'POST',body:JSON.stringify({current_password:$('#current-password').value,new_password:$('#new-password').value})});renderLogin();toast('密码已修改，请重新登录。');}
  else if(action==='new-customer')await newCustomerDialog();
  else if(action==='save-customer'){const c=await api('/api/customers',{method:'POST',body:JSON.stringify({name:$('#customer-name').value.trim(),contact:$('#customer-contact').value.trim(),phone:$('#customer-phone').value.trim(),notes:$('#customer-notes').value.trim(),owner_id:$('#customer-owner')?.value||null})});closeModal();await loadOverview();if(S.view==='audio')S.audioCustomerFilter=c.id;await render();toast('客户档案已创建。');}
  else if(action==='assign-customers')await assignmentDialog([...S.customerSelection]);
  else if(action==='assign-one')await assignmentDialog([el.dataset.id]);
  else if(action==='commit-assignment'){await api('/api/admin/assignments',{method:'POST',body:JSON.stringify({items:S.assignmentItems,owner_id:$('#assignment-owner').value||null,reason:$('#assignment-reason').value.trim()})});closeModal();S.customerSelection.clear();await loadOverview();customersView();toast('客户及相关记录已完成转交。');}
  else if(action==='edit-customer')editCustomerDialog(el.dataset.id);
  else if(action==='delete-customer')deleteCustomerDialog(el.dataset.id);
  else if(action==='update-customer'){
    await api(`/api/customers/${el.dataset.id}`,{method:'PATCH',body:JSON.stringify({expected_revision:Number(el.dataset.revision),name:$('#edit-customer-name').value.trim(),contact:$('#edit-customer-contact').value.trim(),phone:$('#edit-customer-phone').value.trim(),notes:$('#edit-customer-notes').value.trim()})});
    closeModal();await loadOverview();customersView();toast('客户档案已更新。');
  }
  else if(action==='confirm-delete-customer'){
    await api(`/api/customers/${el.dataset.id}?expected_revision=${el.dataset.revision}`,{method:'DELETE'});
    S.customerSelection.delete(el.dataset.id);closeModal();await loadOverview();customersView();toast('客户档案已删除。');
  }
  else if(action==='customer-detail')await customerDetail(el.dataset.id);
  else if(action==='customer-call'){closeModal();await openDetail(el.dataset.id);}
  else if(action==='customer-audio'){S.audioCustomerFilter=el.dataset.id;closeModal();await navigate('audio');}
  else if(action==='customer-analyze'){closeModal();S.draft={title:customerName(el.dataset.id)+' · 新通话',transcript:'',customer_id:el.dataset.id,data_mode:'real',mode:S.config.llm_available?'llm':'rules',call_time:''};S.sampleId='';S.view='customers';await navigate('workspace');}
  else if(action==='new-user')newUserDialog();
  else if(action==='save-user'){await api('/api/admin/users',{method:'POST',body:JSON.stringify({username:$('#user-username').value.trim().toLowerCase(),name:$('#user-name').value.trim(),password:$('#user-password').value,role:$('#user-role').value})});closeModal();await usersView();toast('账号已创建，可使用设置的密码登录。');}
  else if(action==='reset-user'||action==='toggle-user')updateUserDialog(el.dataset.id,action==='reset-user');
  else if(action==='update-user'){const reset=el.dataset.reset==='true';await api(`/api/admin/users/${S.userTarget.id}`,{method:'PATCH',body:JSON.stringify({reason:$('#user-reason').value.trim(),...(reset?{password:$('#reset-password').value}:{active:!S.userTarget.active})})});closeModal();if(S.userTarget.id===S.user.id){renderLogin();toast('密码已重置，请重新登录。');}else{await usersView();toast('账号状态已更新。');}}
  else if(action==='upload-audio'){
    const files=[...($('#audio-file').files||[])],file=files[0],mode=$('[name="audio-customer-mode"]:checked').value,cid=mode==='existing'?$('#audio-customer').value:'';
    if(files.length>1){await batchUploadAudio(el,files);return;}
    if(!file||mode==='existing'&&!cid)throw new Error('请选择录音文件；选择已有客户方式还需指定客户。');
    if(!$('#audio-consent').checked)throw new Error('请确认将录音提交给语音服务转写。');
    if(file.size>S.config.audio_max_bytes)throw new Error(`录音不能超过${S.config.audio_max_bytes/1024/1024} MB。`);
    el.textContent='正在上传…';
    const autoAnalyze=$('#audio-auto-analyze')?.checked;if(autoAnalyze&&mode!=='existing')throw new Error('自动分析请选择已有客户，或使用批量上传。');
    const params=new URLSearchParams({customer_mode:mode,...(cid?{customer_id:cid}:{}),...(autoAnalyze?{auto_analyze:'true'}:{}),filename:file.name,data_mode:$('#audio-data-mode').value,idempotency_key:crypto.randomUUID()});
    const a=await api('/api/transcriptions?'+params,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});await loadOverview();await openAudio(a.id);toast('录音已保存，转写任务已提交。');
  }
  else if(action==='batch-upload-audio')await batchUploadAudio(el);
  else if(action==='retry-auto-analysis'){await api(`/api/transcriptions/${S.audioCurrent.id}/auto-analyze`,{method:'POST'});await openAudio(S.audioCurrent.id);toast('已重新提交自动分析。');}
  else if(action==='refresh-audio-detail')await openAudio(S.audioCurrent.id);
  else if(action==='pick-audio-customer'){
    $('[name="audio-link-mode"][value="existing"]').checked=true;$('#audio-link-existing').hidden=false;$('#audio-link-new').hidden=true;
    $('#audio-link-search').value='';$('#audio-link-customer').innerHTML=customerOptions(el.dataset.id);$('[data-team="link-audio-customer"]').textContent='确认关联客户';
    $$('.identity-candidate').forEach(b=>b.classList.toggle('selected',b===el));
    $('#audio-link-customer').focus();
  }
  else if(action==='identify-audio'){
    if(S.audioDraft.trim()!==(S.audioCurrent.confirmed_text||S.audioCurrent.raw_text).trim())throw new Error('请先保存修改后的校对稿，再重新识别客户。');
    S.audioCurrent=await api(`/api/transcriptions/${S.audioCurrent.id}/identify`,{method:'POST',body:JSON.stringify({expected_revision:S.audioCurrent.revision})});renderAudioDetail();pollAudio(S.audioCurrent.id);
  }
  else if(action==='link-audio-customer'){
    const mode=$('[name="audio-link-mode"]:checked').value;
    const body={expected_revision:S.audioCurrent.revision};
    if(mode==='new'){
      body.new_customer={name:$('#audio-new-name').value.trim(),contact:$('#audio-new-contact').value.trim(),phone:$('#audio-new-phone').value.trim()};
      if(!body.new_customer.name)throw new Error('请核对并填写客户名称。');
    }else{body.customer_id=$('#audio-link-customer').value;if(!body.customer_id)throw new Error('请先选择要关联的客户。');}
    S.audioCurrent=await api(`/api/transcriptions/${S.audioCurrent.id}/customer`,{method:'POST',body:JSON.stringify(body)});await loadOverview();renderAudioDetail();toast('客户归属已确认，录音已归入客户档案。');
  }
  else if(action==='refresh-audio')await audioView();
  else if(action==='clear-audio-filter'){S.audioCustomerFilter=null;await audioView();}
  else if(action==='open-audio'){closeModal();await openAudio(el.dataset.id);}
  else if(action==='confirm-audio'){S.audioDraft=$('#audio-transcript').value;S.audioCurrent=await api(`/api/transcriptions/${S.audioCurrent.id}/confirm`,{method:'POST',body:JSON.stringify({expected_revision:S.audioCurrent.revision,transcript:S.audioDraft})});S.audioDraft=S.audioCurrent.confirmed_text;S.audioDraftDirty=false;renderAudioDetail();toast(S.audioCurrent.auto_analysis_id?'校对稿已保存，已自动提交分析。':'校对稿已保存，可继续分析。');}
  else if(action==='analyze-audio'){const a=S.audioCurrent;if(!a.customer_id||a.status!=='confirmed'||S.audioDraft.trim()!==a.confirmed_text)throw new Error('请先保存当前校对稿。');const created=await api('/api/analyses',{method:'POST',body:JSON.stringify({transcript:a.confirmed_text,title:a.filename,mode:S.config.llm_available?'llm':'rules',data_mode:a.data_mode,call_time:null,idempotency_key:crypto.randomUUID(),customer_id:a.customer_id,transcription_id:a.id})});await loadOverview();await openDetail(created.id);toast('已提交分析。');}
  else if(action==='seek-audio'){const player=$('.audio-player audio');player.currentTime=Number(el.dataset.start);await player.play();}
  else if(action==='retry-audio'){await api(`/api/transcriptions/${S.audioCurrent.id}/retry`,{method:'POST'});await openAudio(S.audioCurrent.id);}
  else if(action==='delete-audio'){showModal(`<div class="modal-head"><h2>删除这份录音？</h2></div><div class="modal-body"><p>${esc(S.audioCurrent.filename)}</p><p class="small-note">将删除本机录音和转写稿。已关联通话分析的录音会被保留。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn danger" data-team="confirm-delete-audio">确认删除</button></div>`);}
  else if(action==='confirm-delete-audio'){await api(`/api/transcriptions/${S.audioCurrent.id}`,{method:'DELETE'});closeModal();await navigate('audio');toast('录音已删除。');}
}

document.addEventListener('submit',e=>{if(e.target.id==='login-form'){e.preventDefault();loginSubmit();}});
document.addEventListener('click',async e=>{const el=e.target.closest('[data-team]');if(!el||el.disabled)return;el.disabled=true;try{await teamAction(el);}catch(err){const target=el.dataset.team==='confirm-audio'?$('#audio-confirm-error'):['upload-audio','batch-upload-audio'].includes(el.dataset.team)?$('#audio-upload-error'):null;if(['link-audio-customer','identify-audio'].includes(el.dataset.team)){const box=$('#audio-identity-error');if(box){box.textContent=err.message;box.hidden=false;}else toast(err.message,true);}else if(target){target.textContent=err.message;target.hidden=false;}else toast(err.message,true);}finally{if(el.isConnected){el.disabled=false;if(el.dataset.team==='upload-audio')el.innerHTML=icon('upload')+' 上传并转写';if(el.dataset.team==='batch-upload-audio')el.innerHTML=icon('upload')+' 批量上传并自动分析';}}});
document.addEventListener('change',e=>{
  if(e.target.name==='audio-customer-mode'){
    const ai=e.target.value==='ai';$('#audio-existing-fields').hidden=ai;$('#audio-customer').disabled=ai;$('#audio-ai-note').hidden=!ai;
    $('#audio-consent').checked=false;const auto=$('#audio-auto-analyze');if(auto){auto.disabled=ai;if(ai)auto.checked=false;}$('#audio-consent-copy').textContent=ai?'将录音提交到已配置的语音服务转写，并将转写内容提交到文字分析模型划分角色和识别客户身份。':'将录音提交到已配置的语音服务转写，并将转写内容提交到文字分析模型划分销售和客户角色。';
  }
  if(e.target.id==='audio-link-customer')$$('.identity-candidate').forEach(b=>b.classList.toggle('selected',b.dataset.id===e.target.value));
  if(e.target.name==='audio-link-mode'){$('#audio-link-existing').hidden=e.target.value==='new';$('#audio-link-new').hidden=e.target.value!=='new';$('[data-team="link-audio-customer"]').textContent=e.target.value==='new'?'确认新建并关联':'确认关联客户';}

  if(e.target.id==='owner-filter'){S.ownerFilter=e.target.value;S.customerSelection.clear();customersView();}
  if(e.target.classList.contains('customer-checkbox')){e.target.checked?S.customerSelection.add(e.target.dataset.id):S.customerSelection.delete(e.target.dataset.id);customersView();}
  if(e.target.id==='all-customers'){S.visibleCustomerIds.forEach(id=>e.target.checked?S.customerSelection.add(id):S.customerSelection.delete(id));customersView();}
  if(e.target.id==='audio-file'){const files=[...(e.target.files||[])];$('#audio-file-name').textContent=files.length?(files.length===1?`${files[0].name} · ${(files[0].size/1024/1024).toFixed(2)} MB`:`已选择 ${files.length} 个文件 · ${(files.reduce((sum,f)=>sum+f.size,0)/1024/1024).toFixed(2)} MB`):'尚未选择文件';}
});
document.addEventListener('input',e=>{
  if(['audio-customer-search','audio-link-search'].includes(e.target.id)){
    const select=$(e.target.id==='audio-customer-search'?'#audio-customer':'#audio-link-customer');
    const query=e.target.value.trim().toLowerCase();
    select.innerHTML=customerOptions(select.value,query);
  }

  if(e.target.id==='customer-search'){const at=e.target.selectionStart;S.customerQuery=e.target.value;customersView();$('#customer-search').focus();$('#customer-search').setSelectionRange(at,at);}
  if(e.target.id==='audio-transcript'){S.audioDraft=e.target.value;S.audioDraftDirty=true;updateAudioReviewControls();}
});

"use strict";

function autoOtherDraft(draft){
  let offset=0;
  return draft.split('\n').map(line=>{
    const m=line.match(/^\s*(待确认|销售|客户)\s*[:：]\s*(.*)$/);
    if(!m){offset+=line.length;return line;}
    const value=m[2].replace(/[\s，,。.!！?？；;：:“”"'、…（）()]/g,'');
    const other=/^(?:[嗯啊哦噢呃唔喂哎诶欸唉]|对{2,})+$/.test(value)||(offset<40&&['家乐来电','酷家乐来电'].includes(value));
    offset+=m[2].length;
    return other?'其他：'+m[2]:line;
  }).join('\n');
}
function audioDraftFor(a){return a.confirmed_text||autoOtherDraft((a.role_status==='ready'?a.role_result?.transcript:'')||a.raw_text);}

function audioDraftReviewState(draft){
  const pending=[],invalid=[];let hasCustomer=false,other=0;
  draft.split('\n').forEach((line,index)=>{
    if(!line.trim())return;
    const uncertain=line.match(/^\s*待确认\s*[:：]\s*(.*)$/);
    if(uncertain){pending.push({index,text:uncertain[1],line});return;}
    const match=line.match(/^\s*(销售|客户|SDR|顾客|销售人员|客服|用户|其他|存疑)\s*[:：]\s*(.*)$/i);
    if(!match||!match[2].trim()){invalid.push(index+1);return;}
    if(['客户','顾客','用户'].includes(match[1]))hasCustomer=true;
    if(match[1]==='其他')other++;
  });
  return {pending,invalid,hasCustomer,other,valid:!pending.length&&!invalid.length&&hasCustomer};
}
function setAudioDraftRole(draft,index,role,expectedLine){
  if(!['销售','客户','其他','存疑'].includes(role))throw new Error('请选择有效角色。');
  const lines=draft.split('\n'),line=lines[index];
  if(line!==expectedLine||!/^\s*待确认\s*[:：]/.test(line))throw new Error('校对稿已变化，请按更新后的内容确认。');
  lines[index]=line.replace(/^\s*待确认\s*[:：]\s*/,role+'：');
  return lines.join('\n');
}
function audioRoleConfirmation(){
  const state=audioDraftReviewState(S.audioDraft),busy=audioBusy(S.audioCurrent);
  S.audioPendingSnapshot={id:S.audioCurrent.id,draft:S.audioDraft};
  return `<div class="role-confirm-header"><h3>逐条确认角色</h3>${pill(state.pending.length?`还剩 ${state.pending.length} 处待确认`:'没有待确认标记',state.pending.length?'amber':'green')}</div>
    <p class="small-note">先回放核对，再为下列语句选择角色。系统提示音选「其他／系统播报」；听不清归属可选「保留存疑」。这两类原句保留，但不参与客户事实和意向分析；选择后还需保存校对稿。</p>
    ${state.pending.length?`<div class="role-confirm-list">${state.pending.slice(0,20).map(p=>`<article class="role-confirm-item"><span class="small-note">校对稿第 ${p.index+1} 行</span><p>${esc(p.text||'（此行没有内容，请在校对区补充或删除空行）')}</p><details class="role-confirm-context"><summary>查看前后句</summary><p>前句：${esc(S.audioDraft.split('\n')[p.index-1]||'（开头）')}</p><p>后句：${esc(S.audioDraft.split('\n')[p.index+1]||'（结尾）')}</p></details><div class="role-confirm-options">${[['销售','设为销售'],['客户','设为客户'],['其他','其他／系统播报'],['存疑','保留存疑']].map(([role,label])=>`<button class="btn secondary" data-team="assign-audio-role" data-line="${p.index}" data-role="${role}" ${busy||!p.text.trim()?'disabled':''}>${label}</button>`).join('')}</div></article>`).join('')}</div>${state.pending.length>20?'<p class="small-note">先显示前 20 处，处理后自动显示其余待确认语句。</p>':''}`:'<p class="role-confirm-complete">角色标记已处理，请继续核对完整对话内容。</p>'}`;
}
function updateAudioReviewControls(){
  const a=S.audioCurrent;if(!a||S.view!=='audioDetail')return;
  const state=audioDraftReviewState(S.audioDraft),busy=audioBusy(a),saved=a.status==='confirmed'&&S.audioDraft.trim()===a.confirmed_text;
  const pending=$('#audio-role-confirmation');if(pending)pending.innerHTML=audioRoleConfirmation();
  const badge=$('#audio-role-state');if(badge&&!['queued','running'].includes(a.role_status))badge.innerHTML=pill(state.pending.length?`${state.pending.length} 处待确认`:saved?'已保存确认':state.valid?'角色已标注 · 待保存':'请检查角色格式',state.pending.length?'amber':saved?'green':'blue');
  const save=$('[data-team="confirm-audio"]');if(save)save.disabled=busy||!state.valid;
  const next=$('[data-team="analyze-audio"]');if(next)next.disabled=busy||!a.customer_id||!saved||!state.valid;
  let message=busy?'正在处理录音，请等待完成。':state.pending.length?`还有 ${state.pending.length} 处待确认，请在上方逐条选择角色。`:state.invalid.length?`第 ${state.invalid.slice(0,5).join('、')} 行缺少有效角色或内容，请在校对框修正。`:!state.hasCustomer?'至少需要一段真实客户发言，不能把全部语句标为销售或其他。':!saved?'角色标记已完成，请核对全文并点击「保存校对并确认角色」。':!a.customer_id?'校对稿已保存，请先在上方确认归属客户。':a.auto_analysis_id&&a.auto_analysis_status==='submitted'?'已自动提交分析，可查看结果；修正后保存校对稿可重新分析。':'校对稿和客户归属已确认，可以直接分析这段录音。';
  const status=$('#audio-next-step');if(status){status.textContent=message;status.className='notice '+(saved&&a.customer_id?'success':'info');}
}
function audioRolesPanel(a){
  if(!['transcribed','confirmed'].includes(a.status))return '';
  const r=a.role_result||{},working=['queued','running'].includes(a.role_status),ready=a.role_status==='ready';
  return `<section class="card audio-roles-card"><div class="card-head"><div><h2>AI 对话整理</h2><p>自动划分销售与客户发言，逐句保留原文</p></div><span id="audio-role-state">${pill(working?'正在分角色':ready?'对话稿已生成':'待生成对话稿',working?'blue':ready?'green':'gray')}</span></div><div class="card-body">
    ${working?`<div class="notice info"><span class="spinner"></span> 正在结合上下文识别销售和客户${r.total_chunks?` · 已处理 ${r.completed_chunks}/${r.total_chunks} 段`:''}。完成后自动填入校对区。</div>`:
      `${a.role_error?`<div class="notice error">${esc(a.role_error)}</div>`:''}
      <p class="role-explanation">${ready?`${r.turns?.length||0} 轮发言已整理。${r.method==='speaker_assisted'?'结合语音服务的说话人分离和对话上下文判断角色。':'根据转写上下文判断角色，当前转写未提供声纹分离信息。'}请核对下方待确认语句，并检查完整对话的角色归属。`:'使用你在「我的模型」设置的文字分析模型，将原始转写整理为“销售：…／客户：…”的逐轮对话。已有校对稿会保留。'}</p>
      <div id="audio-role-confirmation" class="role-confirmation"></div>
      ${ready?`<details class="role-turn-preview"><summary>查看 AI 原始建议 · ${esc(r.model||'')}（修改以校对稿为准）</summary><div>${(r.turns||[]).map(t=>`<article class="role-turn role-${t.role}"><div>${pill({sales:'销售',customer:'客户',unknown:'AI 原始待确认',other:'其他／系统播报'}[t.role],{sales:'blue',customer:'green',unknown:'amber',other:'gray'}[t.role])}${Number.isFinite(t.start)?`<button class="text-button" data-team="seek-audio" data-start="${t.start}">约 ${audioTime(t.start)} · 回放</button>`:''}</div><p>${esc(t.text)}</p>${t.note?`<small>${esc(t.note)}</small>`:''}</article>`).join('')}</div></details>`:''}
      <div class="audio-review-actions"><button class="btn secondary" data-team="roles-audio" ${audioBusy(a)||!S.config.llm_available?'disabled':''}>${ready?'重新识别角色':'AI 自动分角色'}</button>${ready?'<button class="btn secondary" data-team="apply-audio-roles">采用 AI 对话稿</button>':''}</div>
      ${!S.config.llm_available?'<p class="small-note">请先在「我的模型」配置文字分析模型。</p>':''}`}
    </div></section>`;
}

function applyAudioRoles(){
  const r=S.audioCurrent.role_result;
  if(!r?.transcript)return;
  S.audioDraft=autoOtherDraft(r.transcript);S.audioDraftDirty=true;
  renderAudioDetail();toast('AI 对话稿已填入，请核对后保存确认。');
}

async function audioRoleAction(action,el){
  const a=S.audioCurrent;
  if(action==='assign-audio-role'){
    const snapshot=S.audioPendingSnapshot,index=Number(el.dataset.line);
    if(audioBusy(a))throw new Error('正在处理录音，请等待完成。');
    if(snapshot?.id!==a.id||snapshot.draft!==S.audioDraft){updateAudioReviewControls();throw new Error('校对稿已变化，请按更新后的内容确认。');}
    S.audioDraft=setAudioDraftRole(S.audioDraft,index,el.dataset.role,snapshot.draft.split('\n')[index]);S.audioDraftDirty=true;
    $('#audio-transcript').value=S.audioDraft;updateAudioReviewControls();
    toast(`已标为${el.dataset.role}，请核对后保存校对稿。`);
  }else if(action==='roles-audio'){
    const epoch=S.epoch;
    const updated=await api(`/api/transcriptions/${a.id}/roles`,{method:'POST',body:JSON.stringify({expected_revision:a.revision})});
    if(epoch!==S.epoch||S.view!=='audioDetail'||S.audioCurrent?.id!==a.id)return;
    S.audioCurrent=updated;
    renderAudioDetail();pollAudio(a.id);
  }else if(action==='apply-audio-roles'){
    if(S.audioDraftDirty||a.confirmed_text){
      showModal('<div class="modal-head"><h2>采用新的 AI 对话稿？</h2></div><div class="modal-body"><p>将替换编辑框内的文字。已保存的校对稿仍保留，直到你重新保存确认。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-team="accept-audio-roles">替换编辑框内容</button></div>');
    }else applyAudioRoles();
  }else if(action==='accept-audio-roles'){closeModal();applyAudioRoles();}
}

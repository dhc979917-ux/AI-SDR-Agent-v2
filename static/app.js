"use strict";

const icons = {
  audio: '<path d="M3 10v4M7 6v12M11 3v18M15 7v10M19 9v6M23 11v2"/>',
  sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/><path d="M20 2v4M18 4h4"/>',
  inbox: '<path d="M4 4h16l2 11v5H2v-5z"/><path d="M2 14h6l2 3h4l2-3h6"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  chart: '<path d="M4 3v17h17M9 15v-4M14 15V7M19 15V4"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-2l2 3M18 17a7 7 0 0 1-12 2l-2-3"/>',
  switch: '<path d="M4 7h15l-3-3M20 17H5l3 3M19 7l-3 3M5 17l3-3"/>',
  document: '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v5h4M9 12h6M9 16h6"/>',
  arrow: '<path d="M4 12h15m-5-5 5 5-5 5"/>',
  back: '<path d="M20 12H5m5-5-5 5 5 5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  building: '<path d="M4 21V5h10v16M14 10h6v11M2 21h20M7 9h3M7 13h3M7 17h3M17 14h1M17 18h1"/>',
  shield: '<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6Z"/><path d="m8 12 3 3 5-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  upload: '<path d="M12 17V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  link: '<path d="m10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 1 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 -1) scale(.9)"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-3c0-5 16-5 16 0v3"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.document}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = { view: 'workspace', config: {}, samples: [], rows: [], metrics: {}, current: null, detailTab: 'result',
  filter: 'all', query: '', selected: new Set(), draft: null, sampleId: 'verified', busy: false, poll: null,
  epoch:0, csrf:'', user:null, customers:[], users:[], customerSelection:new Set(), ownerFilter:'all', customerQuery:'', audioRows:[], audioCurrent:null, audioDraft:'', audioPoll:null, batchTxtFiles:[], evidenceTouched:false };
sessionStorage.removeItem('sdr_token');sessionStorage.removeItem('sdr_demo_actor');
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const main = $('#main');
const statusLabels = {pending:'待审核',in_review:'审核中',approved:'已通过',deferred:'待补证',rejected:'已排除',cancelled:'已停止',not_required:'无需审核'};
const typeLabels = {sales:'需求沟通',service:'服务请求',invalid:'无效通话',unknown:'信息不足'};
const fieldLabels = {contact_name:'联系人',contact_title:'职位',phone:'手机号',area:'经营地区',industry:'行业',mendian:'门店数',shejishi:'设计师人数',team_count:'设计部人数',raw_company:'口述企业名称',brand_name:'品牌',demand_summary:'客户需求'};
const levelLabels = {high:'高意向',medium:'中意向',low:'低意向'};
const dateText = value => value ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) : '—';
const pill = (text, color='gray') => `<span class="pill ${color}">${esc(text)}</span>`;
const intentPill = value => pill(levelLabels[value] || '尚未判定', value === 'high' ? 'green' : value === 'medium' ? 'blue' : 'gray');
const empty = (title, description) => `<div class="empty">${icon('inbox')}<strong>${esc(title)}</strong><span>${esc(description)}</span></div>`;

async function api(path, options = {}, raw = false) {
  const headers = {...(typeof options.body==='string' ? {'Content-Type':'application/json'} : {}), ...(S.csrf ? {'X-CSRF-Token':S.csrf}:{}), ...options.headers};
  const epoch=S.epoch;
  const response = await fetch(path, {...options, headers});
  if(epoch!==S.epoch)throw new Error('登录状态已变化，请重试。');
  if (!response.ok) {
    const data = await response.json().catch(() => ({detail:`请求失败 (${response.status})`}));
    const message = Array.isArray(data.detail) ? data.detail.map(e => e.msg).join('；') : data.detail;
    if (response.status === 401 && path!=='/api/auth/login') showLogin();
    throw new Error(message || '请求未完成');
  }
  return raw ? response : response.json();
}
function toast(message, error=false) {
  const el = $('#toast'); el.textContent = message; el.className = `toast${error ? ' error' : ''}`; el.hidden = false;
  clearTimeout(S.toastTimer); S.toastTimer = setTimeout(() => {el.hidden=true;}, error ? 7000 : 3500);
}
function decorate() { $$('[data-icon]').forEach(el => el.innerHTML=icon(el.dataset.icon)); }
function updateChrome() {
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.nav === (S.view === 'detail' || S.view === 'progress' ? 'queue' : S.view==='audioDetail'?'audio':S.view)));
  $('#page-label').textContent = {workspace:'分析工作台',queue:'人工审核',knowledge:'反馈知识库',metrics:'运行概览',customers:'客户资源',audio:'录音转写',audioDetail:'转写校对',users:'账号管理',models:'我的模型',detail:'线索详情',progress:'正在分析'}[S.view];
  $('#pending-count').textContent = S.metrics.pending || 0;
  $('#model-status').innerHTML = `<i></i>${esc(S.config.llm_available ? `${S.config.model} 已配置` : '离线规则模式可用')}`;
  $('#actor-name').textContent = S.user?.name || '尚未登录';
  $('#actor-mode').textContent = S.user?.role==='admin' ? '管理员 · 全部客户' : '员工 · 我的客户';
  $$('.admin-only').forEach(el=>el.hidden=S.user?.role!=='admin');
  decorate();
}
async function loadOverview() {
  [S.rows,S.metrics,S.customers] = await Promise.all([api('/api/analyses'),api('/api/metrics'),api('/api/customers')]); updateChrome();
}
function heading(title, subtitle, eyebrow='WORKSPACE', extra='') {
  return `<div class="page-heading"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p class="subtitle">${esc(subtitle)}</p></div>${extra}</div>`;
}
function stats() {
  return `<div class="stats">${[
    ['已分析通话',S.metrics.completed||0,'通','document','从原始对话提取有效信息'],
    ['高意向线索',S.metrics.high||0,'条','chart','优先推进企业版试用与演示'],
    ['待人工处理',S.metrics.pending||0,'条','inbox','为不确定的判断补充证据'],
    ['审核后就绪',S.metrics.ready||0,'条','shield','演示与真实数据分别标记']
  ].map(([name,n,unit,ic,foot])=>`<div class="stat"><div class="stat-label">${name}${icon(ic)}</div><div class="stat-number">${n}<small>${unit}</small></div><div class="stat-foot">${foot}</div></div>`).join('')}</div>`;
}
function getDraft() {
  if (!S.draft) {
    const sample = S.samples.find(s=>s.id===S.sampleId) || S.samples[0];
    S.draft = {...sample, mode:S.config.llm_available?'llm':'rules', call_time:''};
  }
  return S.draft;
}
function workspace() {
  const d=getDraft();
  main.innerHTML = heading('从销售通话，识别企业版商机。','面向酷家乐 XDR 团队，提取客户设计需求、核实企业主体，为试用和演示跟进提供依据。','KUJIALE · XDR WORKSPACE',pill('企业版 · 销售线索','blue')) + stats() + `
  <div class="workspace-grid">
    <section class="card"><div class="card-head"><div><h2><span class="number-icon">01</span>开始一次分析</h2><p>粘贴通话转写，或选择右侧样例体验</p></div><select id="analysis-mode" class="select-mode" aria-label="分析模式"><option value="llm" ${d.mode==='llm'?'selected':''} ${!S.config.llm_available?'disabled':''}>真实模型分析</option><option value="rules" ${d.mode==='rules'?'selected':''}>离线规则演示</option></select></div>
    <div class="card-body"><div class="field"><label class="field-label" for="analysis-customer">归属客户</label><select id="analysis-customer" ${d.transcription_id?'disabled':''}><option value="">新建客户（使用通话名称）</option>${S.customers.map(c=>`<option value="${c.id}" ${d.customer_id===c.id?'selected':''}>${esc(c.name)}${S.user.role==='admin'?` · ${esc(c.owner_name||'待分配')}`:''}</option>`).join('')}</select></div>${d.transcription_id?`<div class="notice info">已关联确认后的录音转写。<button class="text-button" data-team="open-audio" data-id="${d.transcription_id}">返回录音校对 →</button></div><div class="divider-space"></div>`:''}<div class="field"><label class="field-label" for="call-title">通话名称</label><input id="call-title" maxlength="100" value="${esc(d.title||'新通话')}" placeholder="给这次通话一个易于识别的名称"></div>
    <div class="label-row"><label class="field-label" for="transcript">通话转写</label><label class="file-button">${icon('upload')} 导入 .txt（可多选）<input type="file" id="transcript-file" accept=".txt,text/plain" multiple></label></div>
    <textarea id="transcript" ${d.transcription_id?'readonly':''} class="transcript-input" maxlength="${S.config.transcript_max_chars||100000}" spellcheck="false" placeholder="销售：您好，想了解一下您的需求。&#10;客户：……">${esc(d.transcript||'')}</textarea>
    <div class="label-row" style="margin-top:8px"><span class="small-note">建议保留「销售：」「客户：」角色标记；无标记时会自动分角色</span><span id="char-count" class="small-note">${(d.transcript||'').length.toLocaleString()} / ${(S.config.transcript_max_chars||100000).toLocaleString()}</span></div>
    <div class="batch-txt-row"><span id="batch-txt-count" class="small-note">${S.batchTxtFiles?.length?`已选择 ${S.batchTxtFiles.length} 个 txt`:'在上方一次选择多个 txt，即可批量分析'}</span><button class="btn secondary" data-action="batch-analyze-txt" id="batch-txt-button" ${S.batchTxtFiles?.length?'':'disabled'}>批量分析</button></div>
    <div class="divider-space"></div><div class="field-grid"><div class="field"><label class="field-label" for="data-mode">数据类型</label><select id="data-mode" ${d.transcription_id?'disabled':''}><option value="real" ${d.data_mode==='real'?'selected':''}>真实业务数据</option><option value="synthetic" ${d.data_mode==='synthetic'?'selected':''}>合成演示数据</option><option value="snapshot_demo" ${d.data_mode==='snapshot_demo'?'selected':''}>题目 / 脱敏样例</option></select></div><div class="field"><label class="field-label" for="call-time">实际通话时间 <span class="muted">（选填，北京时间）</span></label><input type="datetime-local" id="call-time" value="${esc(d.call_time||'')}"></div></div></div>
    <div class="card-footer"><span class="small-note">${d.mode==='llm'?'将发送脱敏文本到已配置的模型服务':'规则模式能力有限，结果会明确标记'}</span><button class="btn primary" data-action="${S.batchTxtFiles?.length?'batch-analyze-txt':'analyze'}" ${S.busy?'disabled':''}>${S.batchTxtFiles?.length?`${icon('sparkles')} 批量分析 ${S.batchTxtFiles.length} 个文件 ${icon('arrow')}`:`${icon('sparkles')} 分析这段通话 ${icon('arrow')}`}</button></div></section>
    <div class="right-stack"><section class="card"><div class="card-head"><div><h2>从一个场景开始</h2><p>从全屋定制需求，走到线索审核</p></div><span class="caption-chip">${S.samples.length} 个场景</span></div><div class="sample-list">${S.samples.slice(0,5).map((s,i)=>`<button class="sample-card ${S.sampleId===s.id?'selected':''}" data-action="sample" data-id="${s.id}"><span class="sample-symbol">${icon(i===0?'document':i===1?'building':i===4?'shield':'layers')}</span><span class="sample-copy"><strong>${esc(s.title)}</strong><span>${esc(s.tag)}</span></span><span class="arrow">↗</span></button>`).join('')}</div><div class="card-footer"><span class="small-note">还可体验售后、条件表达与越权指令</span><button class="text-button" data-action="all-samples">全部样例 →</button></div></section>
    <section class="flow-card"><h3>理解、核验，再做判断。</h3><div class="flow"><div class="flow-step"><span>${icon('document')}</span><span>提取证据</span></div><span class="flow-arrow">→</span><div class="flow-step"><span>${icon('building')}</span><span>核验主体</span></div><span class="flow-arrow">→</span><div class="flow-step"><span>${icon('chart')}</span><span>判断意向</span></div><span class="flow-arrow">→</span><div class="flow-step"><span>${icon('shield')}</span><span>人工复核</span></div></div><p class="flow-caption">高意向不等于资料完整。主体不明、证据不足和边界判断，都会进入审核。</p></section>
    <div class="tip-line">企业目录中的「青禾」「栖木」「云筑」均为合成主体。题目品牌会先走企查查候选，多候选不自动确认，人工审核后再形成企业全称。</div></div></div>`;
}
function filteredRows() {
  return S.rows.filter(row => (!S.query || [row.title,row.company,row.id].join(' ').toLowerCase().includes(S.query.toLowerCase())) &&
    (S.filter==='all' || S.filter===row.review_display.state || S.filter==='high' && row.intent==='high' || S.filter==='pending' && row.review_display.state==='pending'));
}
function queue() {
  const rows=filteredRows();
  main.innerHTML=heading('核实每一条，交付给销售的线索。','按处理优先级排列；同状态下高意向优先，同意向下较早创建的记录优先。','HUMAN IN THE LOOP',`<button class="btn secondary" data-action="download-csv">${icon('download')} 导出候选列表</button>`) + `
  <div class="toolbar"><div class="tabs">${[['all','全部记录'],['exception','异常'],['secondary','待复核'],['pending','待审核'],['in_review','审核中'],['deferred','待补证'],['approved','已通过'],['rejected','已排除'],['cancelled','已停止'],['not_required','无需审核']].map(([k,v])=>`<button class="tab ${S.filter===k?'active':''}" data-action="filter" data-filter="${k}">${v}</button>`).join('')}</div><input class="search" id="queue-search" value="${esc(S.query)}" placeholder="搜索公司、通话或记录编号" aria-label="搜索审核记录"></div>
  ${S.selected.size?`<div class="batch-bar">已选择 ${S.selected.size} 条记录<span class="small-note">批量修改会先预览影响范围</span><button class="btn secondary" data-action="batch">批量处理</button></div>`:''}
  <section class="card">${rows.length?`<div class="table-wrap"><table><thead><tr><th><input type="checkbox" class="checkbox" id="select-all" aria-label="选择全部显示的记录" ${rows.every(r=>S.selected.has(r.id))?'checked':''}></th><th>通话 / 企业</th><th>客户意向</th><th>审核状态</th><th>需要关注</th><th>创建时间</th><th></th></tr></thead><tbody>${rows.map((r,i)=>`${i===0||rows[i-1].review_display.state!==r.review_display.state?`<tr class="queue-group"><td colspan="7">${reviewBadge(r)}<span>处理顺序 ${r.review_display.rank+1}</span></td></tr>`:''}<tr class="review-row review-${r.review_display.color}"><td><input type="checkbox" class="checkbox row-checkbox" data-id="${r.id}" aria-label="选择${esc(r.title)}" ${S.selected.has(r.id)?'checked':''}></td><td><button class="title-link" data-action="detail" data-id="${r.id}">${esc(r.company||r.title)}</button><small>${esc([...new Set([customerName(r.customer_id),r.title])].join(' · '))} · ${r.mode==='llm'?'模型':'规则'}${r.data_mode!=='real'?' · 演示':''}</small></td><td>${intentPill(r.intent)}${r.score?`<small>${r.score[0]===r.score[1]?r.score[0]:r.score.join('—')} 分</small>`:''}</td><td>${reviewBadge(r)}</td><td><span class="small-note">${r.do_not_contact?'停止联系':r.status==='failed'?'分析失败，可人工处理':esc(reasonShort(r.review_reasons[0]))}</span>${r.review_reasons.length>1?`<small>另有 ${r.review_reasons.length-1} 项</small>`:''}</td><td><span class="small-note">${dateText(r.created_at)}</span></td><td><button class="text-button" data-action="detail" data-id="${r.id}">查看 →</button> ${['queued','running'].includes(r.status)?`<button class="text-button" data-action="cancel-analysis" data-id="${r.id}">停止</button>`:`<button class="text-button" data-action="delete-analysis" data-id="${r.id}" data-revision="${r.revision}">删除</button>`}</td></tr>`).join('')}</tbody></table></div><div class="queue-footer">${rows.length} 条记录 · 已按处理优先级排列 · 日期按北京时间显示</div>`:empty('这里还没有待处理的通话','先在分析工作台运行一条样例，结果会出现在这里。')}</section>`;
}
function reasonShort(code) {
  return {ENTITY_UNRESOLVED:'企业名称待填写',ENTITY_AMBIGUOUS:'主体归属歧义',ENTITY_UNCONFIRMED:'企业全称待确认',ROLE_PENDING:'角色待确认',ENTITY_LOOKUP_FAILED:'检索失败',CONTACT_MASKED:'联系方式脱敏',CONTACT_MISSING:'缺联系方式',CONTACT_NAME_MISSING:'缺联系人',INTENT_INSUFFICIENT:'意向证据不足',INTENT_BOUNDARY:'意向边界判断',EVIDENCE_INVALID:'字段证据有误',MANUAL_REVIEW:'冷启动审核',SECOND_REVIEW:'等待独立复核',EXTRACTION_FAILED:'模型提取失败',QUALIFICATION_UNKNOWN:'企业资格待确认'}[code]||'—';
}
function evidenceLinks(evs=[]) { return `<span class="evidence-links">${[...new Set(evs.map(e=>e.turn_id))].map(t=>`<button class="evidence-link" data-action="evidence" data-turn="${esc(t)}">${esc(t)} ↗</button>`).join(' ')}</span>`; }
function aiEvidenceTurns(r){
  const ids=new Set(),add=evs=>(evs||[]).forEach(e=>{if(e?.turn_id)ids.add(e.turn_id);});
  Object.values(r.extraction?.fields||{}).forEach(f=>add(f.evidence));
  Object.values(r.extraction?.signals||{}).forEach(s=>add(s.evidence));
  add(r.extraction?.classification_evidence);add(r.extraction?.policy_evidence);
  return [...ids].sort((a,b)=>Number(String(a).replace(/\D/g,''))-Number(String(b).replace(/\D/g,''))).slice(0,20);
}
function resultView(record) {
  const r=record.result, f=r.extraction.fields, v=name=>f[name]?.value;
  return `<div class="insight-row"><div class="insight"><div class="label">当前意向</div><div class="value">${levelLabels[r.intent.level]||'待判断'}</div><div class="note">${r.intent.status==='policy'?'明确拒绝规则':r.intent.status==='human_override'?'人工覆盖等级':'基于原文信号'}</div></div><div class="insight"><div class="label">${r.intent.total===undefined?"历史评分区间":"意向得分"}</div><div class="value">${r.intent.total ?? (r.intent.interval[0]===r.intent.interval[1]?r.intent.interval[0]:r.intent.interval.join('–'))}<small> / 100</small></div><div class="note">证据覆盖 ${Math.round(r.intent.coverage*100)}%</div></div><div class="insight"><div class="label">规则可靠性</div><div class="value">${r.quality.overall.toFixed(2)}</div><div class="note">用于审核，不是正确概率</div></div></div>
  ${r.extraction.do_not_contact?'<div class="notice error">客户明确要求停止联系。此记录不进入营销培育或正式 SQL 交付。</div>':''}
  ${record.error?`<div class="notice error">${esc(record.error)}<br><button class="text-button" data-action="retry">重新分析这段通话 →</button></div>`:''}
  ${r.role_pending_count?`<div class="notice">有 ${r.role_pending_count} 处角色待确认标记，已暂不参与事实抽取，可在录音详情中修正。</div>`:''}
  ${r.auto_roles?`<div class="notice info">${r.auto_roles.method==='text_role_model'?`AI 已自动识别销售/客户角色${r.auto_roles.pending_count?`；${r.auto_roles.pending_count} 处角色待确认，已标记为不参与事实抽取`:''}；原文证据已预填，可在审核时增删。`:`未完成自动分角色，已按单段客户发言处理${r.auto_roles.error?`：${esc(r.auto_roles.error)}`:''}。`}</div>`:''}
  <section class="card"><div class="card-body"><div class="section-head"><h3>${icon('building')} 企业抬头</h3>${pill(r.entity.status==='confirmed'&&r.entity.auto_confirmed?'全称自动确认':{captured:'来自通话',manual:'人工填写',missing:'待填写',verified:'已核验',suggested:'全称待确认',confirmed:'全称已确认',ambiguous:'归属待确认',unresolved:'待核验',lookup_failed:'检索失败'}[r.entity.status],['verified','confirmed'].includes(r.entity.status)?'green':'amber')}</div>
  ${r.entity.selected?`<div class="entity-title">${esc(r.entity.selected.name)}</div><div class="entity-meta"><span>${esc(r.entity.selected.area||r.entity.selected.registered_area||'')}</span><span>${esc(r.entity.selected.credit_code||r.entity.selected.id||'')}</span><span>${r.entity.ranker==='llm'?`企查查 MCP + ${esc(r.entity.rerank_model||'文字模型')} 重排`:r.entity.provider==='qcc_mcp'?'企查查 MCP 召回':r.entity.method==='transcript_or_manual'?'按通话或人工填写记录，未进行工商核验':record.request.data_mode!=='real'?'演示核验 · 不代表真实工商记录':''}${r.entity.auto_confirmed?' · 自动确认':''}</span></div>`:`<div class="entity-placeholder">口述名称：<strong>${esc(r.entity.mention||r.entity.raw_name||'尚未提供')}</strong><br>${r.entity.status==='ambiguous'?'有多个可能的经营主体。品牌、城市和行业相似，仍不足以确定客户归属。':r.entity.status==='lookup_failed'?esc(r.entity.error||'企查查检索失败，保留原话。'):'通话未提取到企业名称，请在「审核修正」中自行填写。'}</div>`}
  ${(r.entity.candidates||[]).slice(0,5).map((c,i)=>`<div class="entity-candidate"><div class="candidate-top"><span>${i+1}. ${esc(c.name)}</span><span>${c.rerank_rank?pill(`AI重排 #${c.rerank_rank}`,c.rerank_match==='exact'?'green':c.rerank_match==='unrelated'?'red':'amber'):''}${pill(c.source_type==='qcc_mcp'?'企查查 MCP':c.data_mode==='real'?'企业资料':'合成主体')}</span></div><div class="candidate-info">${esc(c.area||c.registered_area||'地区待查询')} · ${esc(c.industry||'行业待查询')} · ${esc(({brand_owner:'品牌主体',dealer:'经销商',branch:'分支机构',unknown:'角色待确认'})[c.rerank_role]||c.role_hint||({headquarters:'品牌主体',dealer:'经销商',branch:'分支机构',unknown:'角色待核验'})[c.role]||c.role)}<br>${c.credit_code?`信用代码 ${esc(c.credit_code)} · `:''}${c.legal_representative?`法定代表人 ${esc(c.legal_representative)} · `:''}名称匹配 ${Math.round((c.name_match||0)*100)} / 100<br>${c.rerank_confidence!==undefined?`AI 重排 ${Math.round(c.rerank_confidence*100)} / 100 · ${esc(c.rerank_match||'')}<br>${esc(c.rerank_reason||'')}<br>`:''}${esc(c.match_reason||'')}${c.relation_supported?' · 客户关系有原文支持':' · 客户关系待人工确认'}</div><span class="candidate-source">${esc(c.source_title||'')} · ${esc(c.observed_at||'')}${c.source_url&&c.source_url.startsWith('https://')?` · <a href="${esc(c.source_url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>`:''}</span></div>`).join('')}
  ${r.entity.candidates?.length?'<p class="small-note" style="margin-bottom:0">排序分用于比较候选，不代表主体识别正确概率；多候选必须人工确认。</p>':''}</div></section>
  <section class="card"><div class="card-body"><div class="section-head"><h3>${icon('chart')} 意向评分卡</h3>${r.intent.boundary?pill('边界需复核','amber'):pill('可复算','green')}</div>${Object.entries(S.config.dimensions).map(([d,label])=>{const signal=r.extraction.signals[d], n=r.intent.scores[d],max={N:35,T:25,B:20,E:20}[d];return `<div class="score-row"><div class="score-top"><span class="score-name">${label}<span class="muted"> · ${d}</span></span><span class="score-val">${signal.kind==='unknown'?(r.intent.total===undefined?'未知':'0（证据缺失）'):n}<small> / ${max}</small></span></div><div class="score-track"><div class="score-fill" style="width:${n===null?0:n/max*100}%"></div></div><div class="score-bottom"><span>${esc(S.config.labels[signal.kind])}</span>${evidenceLinks(signal.evidence)}</div></div>`}).join('')}<p class="small-note">未知维度暂计 0 分，保留未知标记，不等同于客户否定。${r.human_level?'当前等级经人工覆盖，评分仍展示原规则结果。':''}</p></div></section>
  <section class="card"><div class="card-body"><div class="section-head"><h3>${icon('document')} 已提取事实</h3><span class="small-note">点击 T 编号查看证据</span></div><div class="fact-grid">${['contact_name','contact_title','industry','area','phone','mendian','shejishi','team_count','demand_summary'].map(name=>`<div class="fact ${name==='demand_summary'?'full':''}"><div class="name">${fieldLabels[name]}</div><div class="content">${v(name)===null?'<span class="unknown">尚未确认</span>':esc(v(name))}${f[name]?.qualifier==='approximate'?'（约数）':''} ${evidenceLinks(f[name]?.evidence)}${f[name]?.validation_status==='human_attested'?`<div class="small-note">人工确认 · ${esc(f[name].source_reference||'原文补证')}</div>`:''}</div></div>`).join('')}</div></div></section>
  <section class="card"><div class="card-body"><div class="section-head"><h3>${icon('arrow')} 下一步建议</h3></div><p class="next-action">${esc(r.next_action)}</p>${r.review_reasons.length?`<div class="reason-chips">${r.review_reasons.map(c=>`<span class="reason-chip">${esc(r.reason_labels[c]||c)}</span>`).join('')}</div>`:''}</div></section>`;
}
function reviewView(record) {
  const r=record.result, f=r.extraction.fields;
  return `<section class="card"><div class="card-head"><div><h2>核实事实，再提交判断</h2><p>修改会保存为新修订，原始模型结果始终保留</p></div><span class="caption-chip">修订 v${record.revision}</span></div><form id="review-form" class="card-body review-form">
  ${r.review.stage==='secondary'?`<div class="notice info">此修订等待独立复核。初审人：${esc(r.review.primary_actor)}。需另一名审核人确认。${S.user.role==='employee'?'请由管理员完成独立复核。':'初审与复核必须由不同账号完成。'}</div><div class="divider-space"></div>`:''}
  <div id="review-error" hidden class="form-error" role="alert"></div><div class="field"><label class="field-label" for="review-call-type">通话类型</label><select id="review-call-type">${Object.entries(typeLabels).map(([k,v])=>`<option value="${k}" ${r.extraction.call_type===k?'selected':''}>${v}</option>`).join('')}</select></div>
  <div class="field-grid">${['contact_name','contact_title','phone','industry','area','shejishi','mendian','team_count'].map(name=>`<div class="field"><label class="field-label" for="edit-${name}">${fieldLabels[name]}</label><input id="edit-${name}" data-field="${name}" ${['shejishi','mendian','team_count'].includes(name)?'type="number" min="0" max="100000" step="1"':'maxlength="2000"'} value="${esc(f[name].value??'')}" placeholder="未知留空"></div>`).join('')}</div>
  <hr class="section-divider"><div class="field"><label class="field-label">企业抬头还原</label>
  ${r.entity.candidates?.length?`<div class="entity-pick-list">${r.entity.candidates.map(c=>`<label class="entity-pick"><input type="radio" name="entity-candidate" value="${esc(c.id)}" data-name="${esc(c.name)}" ${r.entity.selected?.id===c.id?'checked':''}><span><strong>${esc(c.name)}</strong><small>${esc(c.credit_code||c.id)} · ${esc(c.status||'')} · ${esc(c.area||c.registered_area||'地区待查询')} · ${esc(c.industry||'行业待查询')}<br>${c.rerank_confidence!==undefined?`AI 重排 ${Math.round(c.rerank_confidence*100)} / 100 · ${esc(c.rerank_match||'')}<br>${esc(c.rerank_reason||'')}<br>`:''}${esc(c.match_reason||'')}</small></span></label>`).join('')}<label class="entity-pick"><input type="radio" name="entity-candidate" value="" data-name="" ${!r.entity.selected&&!r.entity.candidates?.length?'checked':''}><span><strong>使用原话 / 人工填写</strong><small>${esc(r.entity.mention||r.entity.raw_name||'无口述名称')}</small></span></label></div>`:''}
  <label class="field-label" for="review-company-name">企业全称</label><input id="review-company-name" maxlength="2000" value="${esc(r.entity.selected?.name||r.entity.mention||'')}" placeholder="选择候选或人工填写"><p class="small-note">选择企查查候选会记录信用代码和确认人，并进入独立复核；只使用原话时不会宣称工商核验。</p></div>
  <hr class="section-divider"><div class="field"><label class="field-label">人工意向定性</label><select id="manual-intent"><option value="">保留当前判断</option><option value="high">高意向</option><option value="medium">中意向</option><option value="low">低意向</option></select><label><input id="recording-reviewed" type="checkbox" style="width:auto;min-height:0">已核听录音（纯文字记录已核对原文）</label><p class="small-note">0–5分（含5分）必须逐条人工定性；请附原文证据，理由选填。人工定性保留机器分数。</p></div>
  <hr class="section-divider"><label class="field-label">更正意向信号</label><div class="signal-edit-grid">${Object.entries(S.config.tiers).map(([dim,tiers])=>`<div><label class="field-label" for="signal-${dim}">${S.config.dimensions[dim]}</label><select id="signal-${dim}"><option value="unknown" ${r.extraction.signals[dim].kind==='unknown'?'selected':''}>未知 · 不计为否定</option>${Object.entries(tiers).map(([kind,n])=>`<option value="${kind}" ${r.extraction.signals[dim].kind===kind?'selected':''}>${n} · ${esc(S.config.labels[kind])}</option>`).join('')}</select></div>`).join('')}</div>
  <hr class="section-divider"><div class="field"><div class="label-row"><label class="field-label" for="review-evidence">原文证据轮次</label><button type="button" class="text-button" data-action="fill-evidence">重填 AI 证据</button></div><input id="review-evidence" value="${esc(aiEvidenceTurns(r).join(', '))}" placeholder="例如 T04, T06；也可点击左侧「选作证据」"><div class="small-note" style="margin-top:5px">AI 已根据抽取证据预填；可增删，或点击左侧「选作证据」补充。聚焦字段时会带入该字段的原文证据。</div></div><div class="field"><label class="field-label" for="review-source">外部证据来源 <span class="muted">（选填）</span></label><input id="review-source" placeholder="https://… 或 crm:记录编号；演示可用 demo://…"></div><div class="field"><label class="field-label" for="review-reason">审核理由 / 待补证事项 <span class="muted">（选填）</span></label><textarea id="review-reason" rows="3" maxlength="2000" placeholder="说明修正依据，或记录需要向客户核实的问题。"></textarea></div>
  <div class="review-buttons"><button type="button" class="btn secondary" data-action="review-submit" data-decision="save">保存草稿</button><button type="button" class="btn secondary" data-action="review-submit" data-decision="defer">暂缓 · 待补证</button><button type="button" class="btn danger" data-action="review-submit" data-decision="reject">排除无效</button>${r.review.stage==='secondary'?'<button type="button" class="btn primary" data-action="review-submit" data-decision="second_review">独立复核通过</button>':'<button type="button" class="btn primary" data-action="review-submit" data-decision="approve">确认并通过</button>'}</div><p class="small-note">企业名称、联系方式或意向尚未满足要求时无法批准。演示数据不会进入真实 SQL 导出。</p></form></section>`;
}
function auditView(record) {
  return `<section class="card"><div class="card-head"><h2>每一次修改，都可追溯。</h2><span class="small-note">${record.events.length} 条记录</span></div><div class="card-body">${[...record.events].reverse().map(e=>{
    const before=e.before_value, after=e.after_value, diffs=[];
    if(before) for(const [key,label] of Object.entries(fieldLabels)) {const b=before.extraction?.fields[key]?.value,a=after.extraction?.fields[key]?.value;if(JSON.stringify(b)!==JSON.stringify(a))diffs.push(`${label}：${b??'未知'} → ${a??'未知'}`);}
    if(before?.review?.status!==after?.review?.status) diffs.push(`审核状态：${statusLabels[before?.review?.status]||'新建'} → ${statusLabels[after?.review?.status]||'—'}`);
    if(before && before.intent?.level!==after.intent?.level) diffs.push(`意向：${levelLabels[before.intent?.level]||'未知'} → ${levelLabels[after.intent?.level]||'未知'}`);
    return `<div class="audit-event"><div class="audit-title">${esc(e.actor)} · ${esc({analyze:'生成分析',save:'保存草稿',approve:'审核提交',defer:'暂缓补证',reject:'排除无效',second_review:'独立复核',cancel:'停止分析',batch_defer:'批量暂缓',batch_approve:'批量批准',batch_reject:'批量排除',batch_set_intent:'批量订正意向'}[e.action]||e.action)}</div><div class="audit-meta">${dateText(e.created_at)} · 修订 v${e.revision}${e.batch_id?' · 批量事务':''}</div><div class="audit-reason">${esc(e.reason)}</div>${diffs.length?`<div class="audit-diff">${diffs.map(esc).join('<br>')}</div>`:''}</div>`;
  }).join('')}</div></section>`;
}
function detail() {
  const record=S.current, r=record.result;
  if(!r){const stopped=record.status==='cancelled';main.innerHTML=`<button class="back" data-nav="queue">${icon('back')} 返回审核队列</button>`+empty(stopped?'分析已停止':record.status==='interrupted'?'分析已中断':'分析尚未完成',stopped?'原文和检查点已保留，可以重新分析。':record.error||record.phase)+`<button class="btn primary" data-action="retry">重新分析这段通话</button>`;return; }
  main.innerHTML=`<button class="back" data-nav="queue">${icon('back')} 返回审核队列</button>` + heading(record.request.title,`${record.id} · ${dateText(record.created_at)} · 修订 v${record.revision}`,'CALL INSIGHT',`<div class="heading-actions">${record.transcription_id?`<button class="btn secondary" data-team="open-audio" data-id="${record.transcription_id}">查看原录音</button>`:''}<button class="btn secondary" data-action="retry">重新分析</button><button class="btn secondary" data-action="download-json">${icon('download')} 下载候选 JSON</button>${r.production_eligible?'<button class="btn primary" data-action="download-qualified">导出正式 SQL</button>':''}</div>`) + `
  <div class="detail-tags" style="margin-top:-14px;margin-bottom:20px">${intentPill(r.intent.level)}${pill(typeLabels[r.extraction.call_type])}${reviewBadge(record)}${pill(record.request.data_mode==='real'?'真实业务数据':'演示数据')}${pill(record.request.mode==='llm'?`模型 · ${r.provider.model}`:'离线规则 · 非模型判断')}${r.delivery_status==='ready'?pill(record.request.data_mode==='real'?'交付就绪':'演示就绪','green'):''}</div>
  <div class="detail-layout"><section class="card transcript-panel"><div class="card-head"><h2>通话原文</h2><span class="small-note">${r.turns.length} 个轮次 · 点击证据定位</span></div><div class="transcript-list">${r.turns.map(t=>`<div class="turn ${t.speaker}" id="turn-${t.id}"><div class="turn-meta"><span>${t.id}</span><strong>${{customer:'客户',sales:'销售',unknown:'角色待定',other:'其他／系统播报',excluded:'存疑（不参与分析）',pending:'角色待确认（不参与分析）'}[t.speaker]}</strong>${S.detailTab==='review'&&!['other','excluded'].includes(t.speaker)?`<button class="turn-pick" data-action="pick-turn" data-turn="${t.id}">选作证据 +</button>`:''}</div><div class="turn-text">${esc(t.text)}</div></div>`).join('')}</div><div class="card-footer"><span class="small-note">原文保留，不覆盖纠错，不补造缺失内容。</span></div></section>
  <div class="detail-right"><div class="card detail-tabbar">${[['result','分析结果'],['review','审核修正'],['audit','操作记录'],['json','JSON 输出']].map(([k,v])=>`<button class="detail-tab ${S.detailTab===k?'active':''}" data-action="detail-tab" data-tab="${k}">${v}</button>`).join('')}</div>${S.detailTab==='result'?resultView(record):S.detailTab==='review'?reviewView(record):S.detailTab==='audit'?auditView(record):`<section class="card"><div class="card-head"><h2>${r.lead?'结构化线索候选':'未生成销售线索'}</h2>${pill('JSON','green')}</div><div class="card-body"><p class="small-note">下载文件包含证据、评分、版本与审核元数据。${r.lead?'':'当前通话不适用销售线索，或尚无足够证据。'}</p><pre class="json-block">${esc(JSON.stringify(r.lead||{lead:null,call_type:r.extraction.call_type,intent_status:r.intent.status,review_reasons:r.review_reasons},null,2))}</pre></div></section>`}</div></div>`;
}
function progress(record) {
  const labels=['原文分段与角色定位',record.request.mode==='llm'?'模型理解与事实抽取':'离线规则抽取','证据与字段口径校验','企业召回与主体关系核验','意向评分与审核路由'];
  const index=labels.indexOf(record.phase), seconds=Math.max(0,Math.floor((Date.now()-new Date(record.created_at).getTime())/1000));
  main.innerHTML=heading('正在理解这一次对话。','每一步都会留下记录；发现不确定性时，会交给人工确认。','ANALYSIS IN PROGRESS')+`<div class="workspace-grid"><section class="card progress-card"><span class="pill green">${esc(record.request.title)}</span><div class="elapsed">${seconds}<small> 秒</small></div><div class="progress-steps">${labels.map((label,i)=>`<div class="progress-step ${i<index?'done':i===index?'active':''}">${i===index?'<span class="spinner"></span>':`<span class="dot">${i<index?'✓':i+1}</span>`}${label}</div>`).join('')}</div><p class="small-note">${record.request.mode==='llm'?'正在请求真实模型服务。超时或校验失败会显示具体状态，不会静默切换到规则模式。':'当前为离线规则演示，适合复现工作流；开放文本的理解能力有限。'}</p><button class="btn danger" data-action="cancel-analysis" data-id="${record.id}">停止分析</button></section><section class="flow-card"><h3>好的判断，始于准确的证据。</h3><p class="flow-caption" style="margin-top:15px">企业品牌与经营主体分开核验。<br>未知信息保留未知。<br>每个意向分数都回到原始对话。</p></section></div>`;
}
async function knowledge() {
  const rows=await api('/api/feedback'); if(S.view!=='knowledge')return;
  main.innerHTML=heading('让一次修正，成为下一次的经验。','XDR 审核反馈先进入候选区；通过独立验证后用于候选召回或开发样本，支持撤回。','FEEDBACK & LEARNING',pill(`${rows.length} 条反馈`)) + `<div class="notice info" style="margin-bottom:22px">别名只增强候选召回，不会直接证明客户归属。合成主体与真实数据保持隔离；冻结测试集不进入学习库。</div>` + (rows.length?rows.map(f=>`<section class="knowledge-card"><div class="knowledge-header"><span class="knowledge-title">${f.kind==='entity_alias'?`${esc(f.payload.mention)} → ${esc(f.payload.entity_id)}`:f.kind==='intent_override'?'意向等级人工订正':'字段 / 信号修正'}</span>${pill({pending:'待验证',published:'已发布',revoked:'已撤回'}[f.status]||f.status,f.status==='published'?'green':'amber')}</div><p class="knowledge-desc">${f.kind==='entity_alias'?`范围：${esc(f.payload.area)} · 来源线索 ${esc(f.payload.scope)}<br>关系依据：${esc(f.payload.relation_note||'按企查查候选确认')}`:esc(f.payload.reason||'审核提交的字段更正，完整差异见原记录。')}</p><div class="knowledge-footer"><span>${esc(f.actor)} · ${dateText(f.created_at)}${f.validation?` · ${esc(({retrieval_candidates_only:'仅用于候选召回',curated_development_sample:'已审核的开发样本'})[f.validation.usage]||'已验证')}`:''}</span><div class="row-actions"><button class="text-button" data-action="detail" data-id="${f.analysis_id}">查看原记录 ↗</button>${S.user.role==='admin' && f.status==='pending'?`<button class="btn secondary" data-action="feedback-publish" data-id="${f.id}">验证并发布</button>`:S.user.role==='admin' && f.status==='published'?`<button class="btn secondary" data-action="feedback-revoke" data-id="${f.id}">撤回发布</button>`:''}</div></div></section>`).join(''):empty('知识来自经过验证的修正','在审核页更正字段或企业主体后，反馈候选会出现在这里。'));
}
function metrics() {
  const m=S.metrics,total=m.completed||1;
  main.innerHTML=heading('看见系统如何工作。','这些是运行与审核统计。识别准确率需要独立标注集评测，不由人工修改率推断。','OBSERVABILITY',pill('运行统计 · 非准确率','blue'))+stats()+`<div class="metrics-grid"><section class="card"><div class="card-head"><h2>意向分布</h2><span class="small-note">${m.completed||0} 条已完成分析</span></div><div class="card-body">${[['high','高意向'],['medium','中意向'],['low','低意向'],['None','未判断']].map(([k,label])=>`<div class="bar-row"><span class="bar-label">${label}</span><div class="bar-track"><div class="bar-fill" style="width:${(m.distribution?.[k]||0)/total*100}%"></div></div><span class="bar-count">${m.distribution?.[k]||0}</span></div>`).join('')}</div></section><section class="card"><div class="card-head"><h2>处理与反馈</h2></div><div class="card-body"><div class="metric-pairs">${[['平均处理耗时',`${((m.average_ms||0)/1000).toFixed(1)} s`],['模型 Token',Number(m.tokens||0).toLocaleString()],['失败 / 中断',m.failed||0],['反馈候选',m.feedback_count||0]].map(([k,v])=>`<div class="metric-pair"><div class="name">${k}</div><strong>${v}</strong></div>`).join('')}</div><p class="small-note" style="margin-top:25px">规则分析与模型分析耗时合并统计。具体模式、模型版本和调用量可在每条分析的 JSON 中查看。</p></div></section></div><div class="notice info" style="margin-top:22px">项目包含可运行的边界测试与独立评测脚本。真实／合成样本、规则／模型结果分开报告；不把合成演示通过率宣称为真实业务准确率。</div>`;
}
async function render() {
  updateChrome();
  if(S.view==='workspace')workspace(); else if(S.view==='queue')queue(); else if(S.view==='detail')detail();
  else if(S.view==='knowledge')await knowledge(); else if(S.view==='metrics')metrics();
  else if(S.view==='customers')customersView(); else if(S.view==='users')await usersView(); else if(S.view==='audio')await audioView(); else if(S.view==='audioDetail')renderAudioDetail();
  else if(S.view==='models')await modelsView();
}
async function navigate(view) {
  if(S.view==='workspace')saveDraft();
  S.view=view; S.selected.clear();clearTimeout(S.audioPoll);S.audioPoll=null;
  if(view!=='progress') {clearTimeout(S.poll);S.poll=null;}
  await loadOverview(); await render(); window.scrollTo({top:0});
}
function saveDraft() {
  if(!$('#transcript'))return;
  S.draft={...S.draft,customer_id:$('#analysis-customer').value||null,title:$('#call-title').value,transcript:$('#transcript').value,mode:$('#analysis-mode').value,data_mode:$('#data-mode').value,call_time:$('#call-time').value};
}
async function openDetail(id) {
  const record=await api(`/api/analyses/${id}`);S.current=record;S.evidenceTouched=false;
  if(['queued','running'].includes(record.status)) {S.view='progress';updateChrome();progress(record);pollRun(id);}
  else if(record.status==='interrupted'){S.view='detail';updateChrome();detail();toast('原始输入已保留，可以重新分析。',true);}
  else {S.view='detail';S.detailTab='result';updateChrome();detail();}
  window.scrollTo({top:0});
}
async function pollRun(id) {
  clearTimeout(S.poll);
  S.poll=setTimeout(async()=>{try{const record=await api(`/api/analyses/${id}`);S.current=record;if(!['queued','running'].includes(record.status)){await loadOverview();S.view='detail';S.detailTab='result';updateChrome();detail();toast(record.status==='cancelled'?'分析已停止，原文和检查点已保留。':record.status==='failed'?'分析失败，原文已保存，可人工处理。':'分析完成，已保存证据和审核结果。',record.status==='failed');}else{progress(record);pollRun(id);}}catch(e){toast(e.message,true);}},1300);
}
async function analyze() {
  if(S.busy)return;saveDraft();const d=S.draft;
  if(d.transcript.trim().length<2){toast('请先输入通话文本。',true);return;}
  S.busy=true;workspace();
  try{const created=await api('/api/analyses',{method:'POST',body:JSON.stringify({transcript:d.transcript,title:d.title||'新通话',mode:d.mode,data_mode:d.data_mode,call_time:d.call_time?`${d.call_time}:00+08:00`:null,idempotency_key:crypto.randomUUID(),customer_id:d.customer_id||null,transcription_id:d.transcription_id||null})});await openDetail(created.id);}finally{S.busy=false;if(S.view==='workspace')workspace();}
}
async function download(path,name) {
  const response=await api(path,{},true), blob=await response.blob(), url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function showModal(content) {$('#modal-content').innerHTML=content;if(!$('#modal').open)$('#modal').showModal();}
function closeModal() {$('#modal').close();}
function showLogin() { renderLogin(); }
async function submitReview(action) {
  const record=S.current,r=record.result, fields={},signal_kinds={};
  $$('[data-field]').forEach(el=>{let value=el.value.trim();value=value===''?null:['shejishi','mendian','team_count'].includes(el.dataset.field)?Number(value):value;if(value!==r.extraction.fields[el.dataset.field].value)fields[el.dataset.field]=value;});
  Object.keys(S.config.tiers).forEach(dim=>{const kind=$(`#signal-${dim}`).value;if(kind!==r.extraction.signals[dim].kind)signal_kinds[dim]=kind;});
  const body={intent_level:$('#manual-intent').value||null,recording_reviewed:$('#recording-reviewed').checked,expected_revision:record.revision,action,reason:$('#review-reason').value.trim(),fields,signal_kinds,
    evidence_turn_ids:$('#review-evidence').value.split(/[,，\s]+/).filter(Boolean),source_reference:$('#review-source').value.trim(),
    call_type:$('#review-call-type').value!==r.extraction.call_type?$('#review-call-type').value:null};
  const picked=$('input[name="entity-candidate"]:checked'),pickedId=picked?.value||'',currentId=r.entity.selected?.id||'';
  if(r.entity.candidates?.length&&!picked){$('#review-error').textContent='请选择企业候选，或明确选择“使用原话 / 人工填写”。';$('#review-error').hidden=false;return;}
  if(pickedId&&(pickedId!==currentId||r.entity.status==='suggested')){body.entity_id=pickedId;}
  else{const companyName=$('#review-company-name').value.trim(),baseline=r.entity.selected?.name||'';if(companyName!==baseline)body.company_name=companyName||null;}
  $$('.review-buttons button').forEach(el=>el.disabled=true);
  try{await api(`/api/reviews/${record.id}`,{method:'POST',body:JSON.stringify(body)});S.current=await api(`/api/analyses/${record.id}`);await loadOverview();S.detailTab='result';detail();toast('已保存新修订，审核记录可追溯。');}
  catch(e){$('#review-error').textContent=e.message;$('#review-error').hidden=false;}
  finally{$$('.review-buttons button').forEach(el=>el.disabled=false);}
}
async function batchAnalyzeTxt(el){
  const files=S.batchTxtFiles||[];if(!files.length)throw new Error('请先选择要批量分析的 txt 文件。');
  const mode=$('#analysis-mode').value,dataMode=$('#data-mode').value,max=S.config.transcript_max_chars||100000;
  let ok=0;const failed=[];
  for(let i=0;i<files.length;i++){
    const file=files[i];el.textContent=`正在提交 ${i+1}/${files.length}…`;
    try{
      const text=await file.text();
      if(!text.trim()){failed.push(`${file.name}：空文件`);continue;}
      if(text.length>max){failed.push(`${file.name}：超过字符上限`);continue;}
      const title=(file.name.replace(/\.[^.]+$/,'')||'批量通话').slice(0,100);
      let created=null;
      for(let attempt=0;attempt<5;attempt++){
        try{created=await api('/api/analyses',{method:'POST',body:JSON.stringify({transcript:text,title,mode,data_mode:dataMode,call_time:null,idempotency_key:crypto.randomUUID(),customer_id:null,transcription_id:null})});break;}
        catch(e){if(attempt===4||!/(较多|稍后|429)/.test(e.message)){failed.push(`${file.name}：${e.message}`);break;}await new Promise(resolve=>setTimeout(resolve,2000));}
      }
      if(created)ok++;
    }catch(e){failed.push(`${file.name}：${e.message}`);}
  }
  S.batchTxtFiles=[];const picker=$('#transcript-files');if(picker)picker.value='';const count=$('#batch-txt-count');if(count)count.textContent='在上方一次选择多个 txt，即可批量分析';const button=$('#batch-txt-button');if(button){button.textContent='批量分析';button.disabled=true;}
  await loadOverview();S.view='queue';updateChrome();queue();
  toast(`批量分析：已提交 ${ok} 个${failed.length?`，${failed.length} 个失败：${failed.slice(0,2).join('；')}`:''}。`,failed.length>0);
}
function batchDialog() {
  S.batchPreview=null;
  showModal(`<div class="modal-head"><h2>批量处理 ${S.selected.size} 条记录</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div class="field"><label class="field-label" for="batch-action">操作</label><select id="batch-action"><option value="defer">暂缓，等待补充证据</option><option value="approve">批准满足条件的记录</option><option value="reject">排除无效记录</option><option value="set_intent">订正意向等级（随后独立复核）</option></select></div><div class="field" id="batch-level-field" hidden><label class="field-label" for="batch-level">订正后的等级</label><select id="batch-level"><option value="high">高意向</option><option value="medium">中意向</option><option value="low">低意向</option></select></div><div class="field"><label class="field-label" for="batch-reason">适用于每条记录的理由（选填）</label><textarea id="batch-reason" rows="3" placeholder="说明这批修改的共同依据"></textarea></div><div id="batch-impact"></div><p class="small-note">预览包含固定修订号，5分钟内有效。任何一条发生版本冲突，整批不会提交。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-action="batch-preview" id="batch-primary">预览影响范围</button></div>`);
}
async function batchPreview() {
  const action=$('#batch-action').value;
  const body={items:S.rows.filter(r=>S.selected.has(r.id)).map(r=>({id:r.id,revision:r.revision})),action,reason:$('#batch-reason').value.trim(),intent_level:action==='set_intent'?$('#batch-level').value:null};
  const result=await api('/api/reviews/batch/preview',{method:'POST',body:JSON.stringify(body)});S.batchPreview=result;
  $('#batch-impact').innerHTML=`<div class="batch-preview"><table><thead><tr><th>记录</th><th>影响</th></tr></thead><tbody>${result.impact.map(i=>`<tr><td>${esc(i.title)}</td><td>${i.eligible?`${esc(statusLabels[i.before]||i.before)} → ${esc(statusLabels[i.after]||i.after)}`:`<span style="color:#b07161">${esc(i.reason)}</span>`}</td></tr>`).join('')}</tbody></table></div>`;
  $('#batch-primary').textContent=result.can_commit?'确认提交这批修改':'含不符合条件的记录';$('#batch-primary').disabled=!result.can_commit;$('#batch-primary').dataset.action='batch-commit';
}
async function initialize() {
  try {const session=await api('/api/auth/me');S.user=session.user;S.csrf=session.csrf;document.body.classList.remove('logged-out');S.config=await api('/api/config');S.samples=await api('/api/samples');await loadOverview();await render();}
  catch(e){if(!document.body.classList.contains('logged-out')&&!$('#modal').open)main.innerHTML=`<div class="error-page">${empty('暂时无法连接工作空间',e.message)}<button class="btn secondary" data-action="refresh">重新连接</button></div>`;}
}

document.addEventListener('click',async event=>{
  const nav=event.target.closest('[data-nav]');
  if(nav){try{await navigate(nav.dataset.nav);}catch(e){toast(e.message,true);}return;}
  const el=event.target.closest('[data-action]');if(!el)return;
  const action=el.dataset.action;
  try{
    if(action==='refresh'){await initialize();}
    else if(action==='sample'){saveDraft();const sample=S.samples.find(s=>s.id===el.dataset.id);S.sampleId=sample.id;S.batchTxtFiles=[];S.draft={...sample,mode:S.draft?.mode||(S.config.llm_available?'llm':'rules'),call_time:''};closeModal();S.view='workspace';updateChrome();workspace();}
    else if(action==='all-samples'){showModal(`<div class="modal-head"><h2>选择一个场景</h2><button class="icon-button" data-action="close-modal">${icon('close')}</button></div><div class="sample-list">${S.samples.map(s=>`<button class="sample-card" data-action="sample" data-id="${s.id}"><span class="sample-symbol">${icon('document')}</span><span class="sample-copy"><strong>${esc(s.title)}</strong><span>${esc(s.description)}</span></span><span class="arrow">→</span></button>`).join('')}</div>`);}
    else if(action==='analyze')await analyze();
    else if(action==='batch-analyze-txt')await batchAnalyzeTxt(el);
    else if(action==='detail')await openDetail(el.dataset.id);
    else if(action==='cancel-analysis'){showModal(`<div class="modal-head"><h2>停止这次分析？</h2></div><div class="modal-body"><p>已保存的原文、模型结果和检查点会保留；当前任务会标记为已停止，可以重新分析。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn danger" data-action="confirm-cancel-analysis" data-id="${el.dataset.id}">停止分析</button></div>`);}
    else if(action==='confirm-cancel-analysis'){const id=el.dataset.id;clearTimeout(S.poll);await api(`/api/analyses/${id}/cancel`,{method:'POST'});closeModal();await loadOverview();if(S.current?.id===id){await openDetail(id);}else{queue();}toast('分析已停止，原文和检查点已保留。');}
    else if(action==='delete-analysis'){
      showModal(`<div class="modal-head"><h2>删除这条分析记录？</h2></div><div class="modal-body"><p>将删除分析结果、审核历史及其反馈。客户档案和原始录音保留。</p></div><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn danger" data-action="confirm-delete-analysis" data-id="${esc(el.dataset.id)}" data-revision="${esc(el.dataset.revision)}">删除记录</button></div>`);
    }
    else if(action==='confirm-delete-analysis'){
      await api(`/api/analyses/${el.dataset.id}?expected_revision=${el.dataset.revision}`,{method:'DELETE'});
      S.selected.delete(el.dataset.id);closeModal();await loadOverview();queue();toast('分析记录已删除，客户及录音已保留。');
    }
    else if(action==='filter'){S.filter=el.dataset.filter;queue();}
    else if(action==='detail-tab'){S.detailTab=el.dataset.tab;detail();}
    else if(action==='fill-evidence'){const ids=aiEvidenceTurns(S.current.result);$('#review-evidence').value=ids.join(', ');S.evidenceTouched=false;toast('已填入 AI 识别到的证据轮次，可继续补充。');}
    else if(action==='evidence'){$$('.turn').forEach(t=>t.classList.remove('highlight'));const turn=$(`#turn-${el.dataset.turn}`);if(turn){turn.classList.add('highlight');turn.scrollIntoView({behavior:'smooth',block:'center'});}}
    else if(action==='pick-turn'){S.evidenceTouched=true;const field=$('#review-evidence'),ids=new Set(field.value.split(/[,，\s]+/).filter(Boolean));ids.has(el.dataset.turn)?ids.delete(el.dataset.turn):ids.add(el.dataset.turn);field.value=[...ids].slice(0,20).join(', ');$(`#turn-${el.dataset.turn}`).classList.toggle('highlight',ids.has(el.dataset.turn));}
    else if(action==='retry'){const req=S.current.request;const time=req.call_time?new Date(new Date(req.call_time).getTime()+8*3600000).toISOString().slice(0,16):'';S.draft={...req,customer_id:S.current.customer_id,transcription_id:S.current.transcription_id,call_time:time};S.sampleId='';await navigate('workspace');}
    else if(action==='review-submit')await submitReview(el.dataset.decision);
    else if(action==='download-json')await download(`/api/exports/${S.current.id}`,`${S.current.id}.json`);
    else if(action==='download-qualified')await download(`/api/exports/${S.current.id}?kind=qualified`,`${S.current.id}-sql.json`);
    else if(action==='download-csv')await download('/api/export.csv','sdr-candidates.csv');
    else if(action==='close-modal')closeModal();
    else if(action==='batch')batchDialog();
    else if(action==='batch-preview')await batchPreview();
    else if(action==='batch-commit'){el.disabled=true;await api('/api/reviews/batch/commit',{method:'POST',body:JSON.stringify({preview_id:S.batchPreview.id})});closeModal();S.selected.clear();await loadOverview();queue();toast('批量修改已原子提交。');}
    else if(action==='feedback-publish'||action==='feedback-revoke'){await api(`/api/feedback/${el.dataset.id}/${action.endsWith('publish')?'publish':'revoke'}`,{method:'POST'});await knowledge();toast(action.endsWith('publish')?'反馈已验证发布；仅按限定用途使用。':'反馈已撤回，后续检索不再使用。');}
  }catch(e){toast(e.message,true);if(action==='batch-commit')el.disabled=false;}
});
document.addEventListener('focusin',event=>{
  if(S.view!=='detail'||S.detailTab!=='review'||S.evidenceTouched)return;
  const field=event.target.closest?.('[data-field]');if(!field)return;
  const f=S.current?.result?.extraction?.fields?.[field.dataset.field];
  const ids=[...new Set((f?.evidence||[]).map(e=>e.turn_id))].slice(0,20);
  if(ids.length){const input=$('#review-evidence');if(input)input.value=ids.join(', ');}
});
document.addEventListener('input',event=>{
  if(event.target.id==='transcript'){$('#char-count').textContent=`${event.target.value.length.toLocaleString()} / ${(S.config.transcript_max_chars||100000).toLocaleString()}`;S.sampleId='';}
  if(event.target.id==='review-evidence')S.evidenceTouched=true;
  if(event.target.id==='queue-search'){const at=event.target.selectionStart;S.query=event.target.value;queue();$('#queue-search').focus();$('#queue-search').setSelectionRange(at,at);}
  if(['batch-action','batch-reason','batch-level'].includes(event.target.id)&&S.batchPreview){S.batchPreview=null;$('#batch-impact').innerHTML='';$('#batch-primary').dataset.action='batch-preview';$('#batch-primary').textContent='重新预览影响范围';$('#batch-primary').disabled=false;}
});
document.addEventListener('change',async event=>{
  if(event.target.id==='analysis-mode'){saveDraft();workspace();}
  if(event.target.id==='transcript-file'){
    const files=[...(event.target.files||[])],count=$('#batch-txt-count'),button=$('#batch-txt-button');
    if(files.length>1){S.batchTxtFiles=files;if(count)count.textContent=`已选择 ${files.length} 个 txt`;if(button)button.disabled=false;toast('已选择多个 txt，请点击「批量分析」。');return;}
    S.batchTxtFiles=[];if(count)count.textContent='在上方一次选择多个 txt，即可批量分析';if(button)button.disabled=true;
    const file=files[0];if(file){if(file.size>(S.config.transcript_max_chars||100000)*4+3){toast('文件过大，请使用100,000字符以内的转写文本。',true);return;}const text=await file.text();if(text.length>(S.config.transcript_max_chars||100000)){toast('文本超过100,000字符，请分通话导入。',true);return;}S.draft.transcription_id=null;$('#transcript').readOnly=false;$('#data-mode').disabled=false;$('#analysis-customer').disabled=false;$('#transcript').value=text;$('#char-count').textContent=`${text.length.toLocaleString()} / ${(S.config.transcript_max_chars||100000).toLocaleString()}`;$('#data-mode').value='real';S.sampleId='';saveDraft();}
  }
  if(event.target.classList.contains('row-checkbox')){event.target.checked?S.selected.add(event.target.dataset.id):S.selected.delete(event.target.dataset.id);queue();}
  if(event.target.id==='select-all'){filteredRows().forEach(r=>event.target.checked?S.selected.add(r.id):S.selected.delete(r.id));queue();}
  if(event.target.id==='batch-action')$('#batch-level-field').hidden=event.target.value!=='set_intent';
  if(event.target.name==='entity-candidate'){const input=$('#review-company-name');if(input)input.value=event.target.dataset.name||(S.current?.result?.entity?.mention||'');}
});
$('#switch-actor').addEventListener('click',accountDialog);
document.addEventListener('submit',event=>event.preventDefault());
decorate();initialize();

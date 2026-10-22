"use strict";

async function modelsView(){
  const epoch=S.epoch;
  const config=await api('/api/model-settings');
  if(epoch!==S.epoch||S.view!=='models')return;
  S.modelConfig=config;
  main.innerHTML=heading('为你的工作，选择合适的模型。','录音转写和文字分析分别配置，只对当前账号生效。其他员工不会看到你的 API Key。','MY MODELS',pill(S.user.name+' · 个人配置','blue'))+
    `<div class="model-config-grid">${modelConfigCard('audio')}${modelConfigCard('text')}</div>
    <section class="card model-save-card"><div><strong>保存后，新的任务使用你的选择</strong><p class="small-note">处理中的任务保持启动时的模型；历史结果保留。客户身份识别使用文字分析模型。</p><p class="small-note">连接测试只发送固定的合成示例，可能产生少量调用费用。测试不会保存设置，也不会发送客户记录。</p></div><button class="btn primary" data-model-action="save">${icon('check')} 保存我的配置</button></section>
    <div id="model-save-result" role="status" hidden></div>`;
}

function modelConfigCard(kind,chosen){
  const original=S.modelConfig.profiles[kind],p=chosen||original;
  const providers=S.modelConfig.providers;
  const provider=providers[p.provider]?.[kind]?.length?p.provider:(kind==='text'?'qwen_cn':'qwen_cn');
  const models=providers[provider][kind],known=models.includes(p.model),isDefault=p.use_default;
  const keyless=provider==='funasr_local';
  const sameSaved=!original.use_default&&original.provider===provider&&original.configured&&!keyless;
  return `<section class="card model-config-card" id="model-card-${kind}" data-kind="${kind}">
    <div class="card-head"><div class="model-card-heading"><span class="model-icon">${icon(kind==='audio'?'audio':'sparkles')}</span><div><h2>${kind==='audio'?'录音转写':'文字分析'}</h2><p>${kind==='audio'?'把通话录音转换为文字':'提取客户身份、企业信息与意向证据'}</p></div></div>${pill(isDefault?'工作区默认':'个人选择',isDefault?'gray':'blue')}</div>
    <div class="card-body"><label class="model-personal-toggle"><input type="checkbox" id="model-${kind}-personal" data-model-field="personal" ${!isDefault?'checked':''}> 使用我的模型和 API Key</label>
    <p class="small-note model-default-note">${isDefault?`当前使用：${esc(original.provider_name)} / ${esc(original.model)}${original.configured?'':'（未配置密钥）'}。勾选后可设置个人模型。`:'配置按账号保存，录音和文字可以选择不同厂商。'}</p>
    <fieldset class="model-fields" ${isDefault?'disabled':''}>
      <div class="field"><label class="field-label" for="model-${kind}-provider">模型厂商 / 地区</label><select id="model-${kind}-provider" data-model-field="provider">${Object.entries(providers).filter(([,v])=>v[kind].length).map(([id,v])=>`<option value="${id}" ${id===provider?'selected':''}>${esc(v.name)}</option>`).join('')}</select></div>
      <div class="field"><label class="field-label" for="model-${kind}-select">${kind==='audio'?'转写模型':'分析模型'}</label><select id="model-${kind}-select" data-model-field="model">${models.map(id=>`<option value="${id}" ${id===p.model?'selected':''}>${esc(id)}</option>`).join('')}<option value="custom" ${!known?'selected':''}>输入其他兼容模型 ID…</option></select></div>
      <div class="field" id="model-${kind}-custom-wrap" ${known?'hidden':''}><label class="field-label" for="model-${kind}-custom">模型 ID</label><input id="model-${kind}-custom" maxlength="120" spellcheck="false" value="${esc(known?'':p.model)}" placeholder="填写厂商提供的完整模型 ID"><p class="small-note">${kind==='audio'?'仅支持当前转写接口的模型与快照版本。':'需要支持 Chat Completions 和 JSON 输出。'}模型权限以你的厂商账号为准。</p></div>
      ${keyless?`<p class="small-note model-keyless-note">FunASR 在本机进程内运行转写，不上传录音，也无需 API Key。首次使用需安装依赖并下载模型。</p>`:`<div class="field"><label class="field-label" for="model-${kind}-key">API Key ${sameSaved?'<span class="model-key-saved">已保存 · 不回显</span>':''}</label><input id="model-${kind}-key" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096" placeholder="${sameSaved?'留空保留已保存的密钥':'填写该厂商、该地区的 API Key'}"><p class="small-note">密钥仅发送到本机服务加密保存，不写入浏览器存储。更换厂商或地区后需要重新填写。</p></div>
      ${sameSaved?`<label class="model-clear-key"><input type="checkbox" id="model-${kind}-clear"> 清除这项个人密钥</label>`:''}`}
    </fieldset>
    <div class="model-test-row"><button class="btn secondary" data-model-action="test" data-kind="${kind}">${icon('link')} 测试${kind==='audio'?'转写':'分析'}连接</button><span class="small-note">${kind==='audio'?'固定合成录音':'固定 JSON 示例'}</span></div>
    <div id="model-${kind}-result" class="model-test-result" role="status" hidden></div></div></section>`;
}

function modelProfileFromForm(kind){
  if(!$('#model-'+kind+'-personal').checked)return {use_default:true};
  const selected=$('#model-'+kind+'-select').value;
  return {use_default:false,provider:$('#model-'+kind+'-provider').value,
    model:selected==='custom'?$('#model-'+kind+'-custom').value.trim():selected,
    api_key:$('#model-'+kind+'-key')?.value||null,clear_key:!!$('#model-'+kind+'-clear')?.checked};
}

document.addEventListener('change',e=>{
  if(!e.target.dataset.modelField)return;
  const kind=e.target.closest('[data-kind]').dataset.kind,field=e.target.dataset.modelField;
  if(field==='model')$('#model-'+kind+'-custom-wrap').hidden=e.target.value!=='custom';
  if(field==='personal'||field==='provider'){
    const provider=$('#model-'+kind+'-provider').value;
    const original=S.modelConfig.profiles[kind];
    const model=field==='provider'?S.modelConfig.providers[provider][kind][0]:
      (original.provider===provider?original.model:S.modelConfig.providers[provider][kind][0]);
    $('#model-card-'+kind).outerHTML=modelConfigCard(kind,{provider,model,use_default:!$('#model-'+kind+'-personal').checked});
  }
  const result=$('#model-'+kind+'-result');if(result)result.hidden=true;
  const saved=$('#model-save-result');if(saved)saved.hidden=true;
});
document.addEventListener('input',e=>{
  const card=e.target.closest('.model-config-card');
  if(card){$('#model-'+card.dataset.kind+'-result').hidden=true;$('#model-save-result').hidden=true;}
});
document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-model-action]');if(!button||button.disabled)return;
  const epoch=S.epoch,kind=button.dataset.kind,action=button.dataset.modelAction;
  const revision=S.modelConfig.revision;
  const body=action==='test'?{expected_revision:revision,kind,profile:modelProfileFromForm(kind)}:
    {expected_revision:revision,text:modelProfileFromForm('text'),audio:modelProfileFromForm('audio')};
  const buttons=$$('[data-model-action]');buttons.forEach(b=>b.disabled=true);
  const original=button.innerHTML;button.textContent=action==='test'?'正在测试…':'正在保存…';
  const fields=$$('.model-config-card input,.model-config-card select');
  const disabled=fields.map(f=>f.disabled);fields.forEach(f=>f.disabled=true);
  try{
    const result=await api(action==='test'?'/api/model-settings/test':'/api/model-settings',{
      method:action==='test'?'POST':'PUT',body:JSON.stringify(body)});
    if(epoch!==S.epoch||S.view!=='models')return;
    if(action==='test'){
      const target=$('#model-'+kind+'-result');target.className='notice success model-test-result';target.hidden=false;
      target.textContent=`连接成功 · ${result.model} · ${(result.elapsed_ms/1000).toFixed(1)}秒。此项测试配置尚未保存。`;
    }else{
      const config=await api('/api/config');
      if(epoch!==S.epoch||S.view!=='models')return;
      S.config=config;updateChrome();await modelsView();
      if(epoch!==S.epoch||S.view!=='models')return;
      const target=$('#model-save-result');target.hidden=false;target.className='notice success';
      target.textContent='已保存你的配置，新任务将使用所选模型。';
    }
  }catch(error){
    if(epoch!==S.epoch||S.view!=='models')return;
    const target=$(action==='test'?'#model-'+kind+'-result':'#model-save-result');
    target.hidden=false;target.className='notice error model-test-result';target.textContent=error.message;
  }finally{
    if(button.isConnected)button.innerHTML=original;
    buttons.forEach(b=>{if(b.isConnected)b.disabled=false;});
    fields.forEach((f,i)=>{if(f.isConnected)f.disabled=disabled[i];});
  }
});

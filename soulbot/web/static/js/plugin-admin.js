// Host-rendered forms and escaped results; plugins cannot inject HTML or browser code.
export function createPluginAdminUI(ctx, root, active) {
 const {esc, send, guarded, confirmAction} = ctx;
 let item=null, cards=[], busy=false;
 const safeLink=value=>{try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:null;}catch{return null;}};
 function renderResult(result){
  cards=Array.isArray(result.cards)?result.cards.slice(0,20):[];
  root.querySelector('[data-admin-result]').innerHTML=`<p class="note">${esc(result.message||'操作已完成。')}</p>${cards.length?`<div class="plugin-grid">${cards.map((card,index)=>`<article class="panel plugin-card"><h3>${esc(card.title||'服务')}</h3><p class="row-detail">${esc(card.status||'')} · ${esc(card.subtitle||'')}</p><dl>${(card.fields||[]).slice(0,30).map(f=>`<dt class="muted">${esc(f.label)}</dt><dd class="memory-text">${esc(String(f.value??'—'))}</dd>`).join('')}</dl><div class="actions"><button type="button" class="button quiet small" data-admin-config="${esc(card.title||'')}">配置此服务</button>${(card.actions||[]).filter(a=>item.admin_actions.some(d=>d.name===a.name)).map(a=>`<button type="button" class="button outline small" data-card-index="${index}" data-card-action="${esc(a.name)}">${esc(item.admin_actions.find(d=>d.name===a.name).label)}</button>`).join('')}${(card.links||[]).map(link=>{const href=safeLink(link.url);return href?`<a class="button quiet small" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>`:'';}).join('')}</div></article>`).join('')}</div>`:''}${typeof result.text==='string'?`<pre class="plugin-admin-log" tabindex="0" aria-label="脱敏日志">${esc(result.text)}</pre>`:''}${!cards.length&&!('text' in result)?`<pre class="plugin-admin-log">${esc(JSON.stringify(result,null,2))}</pre>`:''}`;
 }
 async function execute(action,values,button){
  if(busy||!item||!active())return;
  const selected=item;
  const run=()=>guarded(async()=>{
   if(busy||!active()||selected!==item)return;
   busy=true;root.setAttribute('aria-busy','true');root.querySelectorAll('button').forEach(b=>b.disabled=true);
   const output=root.querySelector('[data-admin-result]');output.textContent=action.mutating?'正在执行，请稍候。启动超时后请刷新状态，不要重复操作。':'正在读取服务信息…';
   try{const result=await send(`/api/plugins/${selected.id}/actions/${action.name}`,'POST',{revision:selected.revision,values,confirmed:action.mutating});if(active()&&item===selected)renderResult(result);}
   finally{busy=false;if(active()){root.setAttribute('aria-busy','false');root.querySelectorAll('button').forEach(b=>b.disabled=false);}}
  },button);
  if(action.mutating)confirmAction(action.label+'？',action.description+' 仅使用管理员已保存的配置；此操作不会取得模型工具审批。',run);else await run();
 }
 function show(next){
  item=next;cards=[];const actions=next?.admin_actions||[];
  root.hidden=!next?.enabled||!actions.length;
  if(root.hidden){root.replaceChildren();return;}
  root.innerHTML=`<div class="section-title"><h3>插件服务管理</h3></div><p class="note">仅管理员可执行这些操作。启动、停止和重启需单独确认，不向模型提供命令执行能力。修改配置后请先保存。</p><form data-admin-form data-transient><div class="form-grid"><div class="field"><label for="plugin-admin-action">选择操作</label><select id="plugin-admin-action">${actions.map(a=>`<option value="${esc(a.name)}">${esc(a.label)}</option>`).join('')}</select></div><div data-admin-fields class="form-grid"></div></div><p data-admin-description class="note"></p><button class="button outline" type="submit">执行所选操作</button></form><section data-admin-result class="media-section" aria-live="polite"></section>`;
  const select=root.querySelector('#plugin-admin-action');
  function fields(){const action=actions.find(a=>a.name===select.value);root.querySelector('[data-admin-description]').textContent=action.description;root.querySelector('[data-admin-fields]').innerHTML=Object.entries(action.schema.properties||{}).map(([name,p])=>{const id='admin-arg-'+name;const required=(action.schema.required||[]).includes(name);return `<div class="field"><label for="${esc(id)}">${esc(name==='service'?'服务':p.title||name)}</label>${p.enum?`<select id="${esc(id)}" name="${esc(name)}" ${required?'required':''}>${p.enum.map(value=>`<option value="${esc(value)}">${esc(({napcat:'NapCat',comfyui:'ComfyUI',meme:'表情包生成',tts:'本地 TTS',llm:'本地 LLM',custom:'自定义服务'})[value]||value)}</option>`).join('')}</select>`:`<input id="${esc(id)}" name="${esc(name)}" type="${['integer','number'].includes(p.type)?'number':p.type==='boolean'?'checkbox':'text'}" ${required&&p.type!=='boolean'?'required':''} ${p.maxLength?`maxlength="${p.maxLength}"`:''}>`}</div>`;}).join('');}
  select.onchange=fields;fields();
  root.querySelector('[data-admin-form]').onsubmit=event=>{event.preventDefault();const action=actions.find(a=>a.name===select.value),values={};for(const [name,p] of Object.entries(action.schema.properties||{})){const input=root.querySelector('[data-admin-fields]').querySelector(`[name="${CSS.escape(name)}"]`);values[name]=p.type==='boolean'?input.checked:['integer','number'].includes(p.type)?Number(input.value):input.value;}execute(action,values,event.submitter);};
  root.querySelector('[data-admin-result]').onclick=event=>{const config=event.target.closest('[data-admin-config]');if(config){const input=document.querySelector('#plugin-param-filter');input.value=config.dataset.adminConfig;input.dispatchEvent(new Event('input'));input.scrollIntoView({block:'center'});input.focus();return;}const button=event.target.closest('[data-card-action]');if(!button)return;const descriptor=cards[Number(button.dataset.cardIndex)]?.actions?.find(a=>a.name===button.dataset.cardAction);const action=actions.find(a=>a.name===descriptor?.name);if(action)execute(action,descriptor.values||{},button);};
  const first=actions.find(a=>a.name==='overview'&&!a.mutating&&!(a.schema.required||[]).length);
  if(first)execute(first,{},root.querySelector('button[type="submit"]'));
 }
 return {show};
}

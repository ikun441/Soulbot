import {createPageSession} from './page-session.js';
let session=null;
const $=s=>document.querySelector(s);
export function stopCompletionUI(){session?.dispose();session=null;}

export async function searchPage(ctx){
 const {mount,heading,field,toggle,esc,api,send,guarded,notice,saveEditor,confirmAction}=ctx;
 mount(`${heading('SEARCH & EXPLORATION','好奇，从有出处的信息开始。','搜索只返回网页摘要，自主探索只保存在本地，不会自动发给群友。')}
 <form id="editor" class="panel">${toggle('启用网页搜索','search.enabled','使用你配置的 SearXNG 实例；搜索词会发送给该服务。')}
 <div class="form-grid">${field('SearXNG 地址','search.base_url','需在实例 settings.yml 中启用 search.formats 的 json 格式。','url','required')}${field('每次请求费用（元）','search.price_cny_per_request','自建且无 API 费用时填 0；不确定留空，自动探索会暂停。','number','min="0" max="100" step="any" data-nullable="true"')}${field('最多结果数','search.max_results','','number','min="1" max="10" required')}${field('每分钟最多调用','search.max_calls_per_minute','','number','min="1" max="60" required')}${field('每日最多调用','search.max_calls_per_day','','number','min="1" max="1000" required')}</div>
 ${toggle('自主探索公开主题','exploration.enabled','默认关闭，遵守费用、每日额度、夜间免打扰和全局暂停；不访问私人记忆。')}
 <div class="form-grid">${field('探索主题','exploration.topics','用逗号或换行分隔，只填公开主题。','text','data-list="true" required')}${field('探索间隔（分钟）','exploration.interval_minutes','每次额外加入随机延迟，重启保留已安排时间。','number','min="10" max="10080" required')}${field('每日探索次数','exploration.max_daily_tasks','','number','min="1" max="20" required')}</div><button class="button primary" id="save">保存搜索规则</button></form>
 <section class="panel media-section"><form id="search-form" data-transient><label for="search-query">搜索公开信息</label><input id="search-query" required maxlength="200" placeholder="例如：天文科普"><div class="actions"><button class="button outline">保存并搜索</button></div></form><p id="search-notice" class="note" aria-live="polite"></p><div id="search-results"></div></section>
 <section class="panel media-section"><h2>本地探索记录</h2><p id="explore-state" class="note"></p><div id="explore-notes"></div></section>`);
 const lifecycle=createPageSession($('#search-form'));session=lifecycle;
 const resultHTML=items=>items.map(item=>`<article class="release"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title||item.url)}</a><p class="memory-text">${esc(item.snippet)}</p></article>`).join('')||'<p class="empty-state">服务没有返回可用的公开结果。</p>';
 async function notes(){const data=await api('/api/exploration');if(!lifecycle.active())return;$('#explore-state').textContent=`${data.paused?'全局自动化已暂停':data.enabled?'自主探索已开启':'自主探索关闭'} · 最近状态：${data.state.status||'尚未执行'}`;$('#explore-notes').innerHTML=data.items.map(item=>`<article class="release"><h3>${esc(item.topic)}</h3>${resultHTML(item.results)}<button class="button quiet small" data-remove="${item.id}">删除记录</button></article>`).join('')||'<p class="empty-state">尚无自主探索记录。</p>';}
 $('#search-form').onsubmit=e=>{e.preventDefault();guarded(()=>lifecycle.run(async()=>{if(!await saveEditor())return;const data=await send('/api/search','POST',{query:$('#search-query').value});if(!lifecycle.active())return;$('#search-notice').textContent=data.notice;$('#search-results').innerHTML=resultHTML(data.items);notice('搜索服务已返回真实结果。');}),e.submitter);};
 $('#explore-notes').onclick=e=>{const b=e.target.closest('[data-remove]');if(!b)return;confirmAction('删除这条探索记录？','删除本地资料，不触发外部操作。',async()=>{await send('/api/exploration/'+b.dataset.remove,'DELETE');if(lifecycle.active())await notes();});};
 await notes();lifecycle.repeat(notes,10000);
}

export async function systemPage(ctx){
 const {mount,heading,toggle,api,send,guarded,notice,confirmAction,esc}=ctx;
 mount(`${heading('SYSTEM & RECOVERY','让运行和恢复，都有据可查。','备份包含账号、密钥和私人数据，请保存在自己的设备。')}
 <section class="panel"><h2>全局自主行为</h2><p class="note">统一暂停主动私聊、群聊插话、空间自主发布、探索及自动日记归档/推送。已经发出的请求无法撤回。</p><button class="button primary" id="automation-stop">暂停全部自主行为</button><form id="editor" class="media-section">${toggle('暂停全部自主行为','automation.paused','取消勾选并保存后恢复各功能原有规则；不会自动启用原先关闭的功能。')}<button id="save" class="button outline">保存全局开关</button></form></section>
 <section class="panel media-section"><h2>工具审批队列</h2><p class="note">审核后执行保存的单次参数。结果仅留在本地，审批不自动向 QQ 发送；过期和不确定任务不会重试。</p><div id="tool-approvals"></div></section>
 <section class="panel media-section"><h2>私有完整备份</h2><p class="note">包含 SQLite 数据和额度、配置、.env、密钥、本地表情与壁纸；不含虚拟环境、日志、第三方缓存及临时媒体。最大 256MB。</p><button class="button outline" id="backup-download">生成并下载备份 ZIP</button><h3>恢复备份</h3><p class="row-detail">上传后先验证，下一次停止并重新启动 SoulBot 时应用。自动保留恢复前备份、退出旧会话，并暂停自主行为；需用备份中的账号重新登录。</p><form id="restore-form" data-transient><label for="restore-file">选择 SoulBot 备份</label><input type="file" id="restore-file" accept=".zip" required><button class="button outline">校验并安排恢复</button></form><p id="restore-state" class="note" aria-live="polite"></p><button class="button quiet" id="restore-cancel">取消待恢复备份</button></section>`);
 const lifecycle=createPageSession($('#restore-form'));session=lifecycle;
 async function approvalList(){const data=await api('/api/tool-approvals');if(!lifecycle.active())return;$('#tool-approvals').innerHTML=data.items.map(item=>`<article class="release"><h3>${esc(item.name)} · ${esc(item.status)}</h3><pre class="memory-text">${esc(JSON.stringify(item.arguments,null,2))}</pre>${item.result?`<pre class="memory-text">${esc(JSON.stringify(item.result,null,2))}</pre>`:''}${item.status==='pending'?`<button class="button outline small" data-approve="${item.id}" data-revision="${item.revision}">审批并执行一次</button><button class="button quiet small" data-reject="${item.id}" data-revision="${item.revision}">拒绝</button>`:''}</article>`).join('')||'<p class="empty-state">没有工具审批记录。</p>';}
 $('#tool-approvals').onclick=e=>{const b=e.target.closest('[data-approve],[data-reject]');if(!b)return;confirmAction(b.dataset.approve?'审批执行此工具？':'拒绝这个工具请求？','只执行展示的单次参数，不向 QQ 自动发送。服务调用可能计费。',async()=>{await send('/api/tool-approvals/'+(b.dataset.approve||b.dataset.reject)+'/review','POST',{revision:Number(b.dataset.revision),approve:Boolean(b.dataset.approve)});if(lifecycle.active())await approvalList();});};
 async function state(){const data=await api('/api/backups/status');if(!lifecycle.active())return;$('#restore-state').textContent=data.pending_restore?'已有备份等待下次重启恢复。':'没有等待恢复的备份。';$('#restore-cancel').disabled=!data.pending_restore;}
 $('#automation-stop').onclick=()=>confirmAction('暂停全部自主行为？','立即保存暂停标记，正在发送的请求可能已经到达平台。',async()=>{await send('/api/automation/stop','POST');ctx.config().automation.paused=true;if(lifecycle.active()){document.querySelector('[data-field="automation.paused"]').checked=true;notice('全部自主行为已暂停，请保存全局开关后再恢复。');}});
 $('#backup-download').onclick=e=>guarded(()=>lifecycle.run(async()=>{const r=await fetch('/api/backups',{method:'POST',headers:{'X-Soulbot-Request':'1'},credentials:'same-origin'});if(!r.ok)throw new Error((await r.json()).detail||'备份失败');const blob=await r.blob();if(!lifecycle.active())return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='soulbot-private-backup.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('备份已生成，请保管好下载文件。');}),e.currentTarget);
 $('#restore-form').onsubmit=e=>{e.preventDefault();const file=$('#restore-file').files[0];if(!file)return;if(file.size>256*1024*1024){notice('备份不能超过 256MB',true);return;}confirmAction('在下次启动时恢复这个备份？','当前数据会先备份。恢复后需重新登录并检查配置，自动化会暂停。',async()=>{const body=new FormData();body.append('file',file);const data=await api('/api/backups/restore',{method:'POST',body});if(lifecycle.active()){notice(data.message);await state();}});};
 $('#restore-cancel').onclick=e=>guarded(async()=>{await send('/api/backups/restore','DELETE');if(lifecycle.active())await state();},e.currentTarget);
 await Promise.all([state(),approvalList()]);lifecycle.repeat(approvalList,10000);
}

export async function bindPersonality(ctx){
 const {api,send,guarded,confirmAction,notice}=ctx;
 const anchor=$('#personality-extras');if(!anchor)return;
 const data=await api('/api/personality/templates');if(!anchor.isConnected)return;
 const select=$('#personality-template');for(const item of data.items){const option=document.createElement('option');option.value=item.id;option.textContent=item.id==='cat'?'温柔猫娘':item.id==='curious'?'好奇伙伴':'温柔日常';select.append(option);}
 const apply=payload=>confirmAction('应用这份人格？','将替换名称、描述、风格、兴趣和 Prompt，主人 QQ 与称呼保留。',async()=>{await send('/api/personality','PUT',payload);location.reload();});
 $('#personality-apply').onclick=()=>{const item=data.items.find(item=>item.id===select.value);if(item){const {id,...payload}=item;apply(payload);}};
 $('#personality-import').onchange=e=>guarded(async()=>{const file=e.target.files[0];if(!file)return;if(file.size>24000)throw new Error('人格 JSON 不能超过 24KB');const payload=JSON.parse(await file.text());apply(payload);},e.target);
 $('#personality-preview').onclick=e=>guarded(async()=>{const p=await api('/api/personality/preview');if(anchor.isConnected){$('#personality-prompt').textContent=p.content;notice('显示当前已保存的人格预览');}},e.currentTarget);
}

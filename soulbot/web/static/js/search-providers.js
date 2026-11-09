import {providerStatusText} from './provider-guidance.js?v=v2.7.0';
const labels={primary:'主搜索服务',fallback_1:'备用搜索服务 1',fallback_2:'备用搜索服务 2'};
export function searchProvidersForm(ctx){
 const {config,field,toggle}=ctx;
 const cfg=config().search;
 cfg.fallbacks ||= {};
 for(let i=1;i<=2;i++)cfg.fallbacks['fallback_'+i] ||= {enabled:false,base_url:'http://127.0.0.1:'+(8080+i),timeout_seconds:15,price_cny_per_request:null};
 return `<details class="media-section"><summary>高级：备用搜索服务与超时</summary><p class="note">主服务失败后按顺序尝试已启用的备用服务，每个地址最多尝试一次。超时或无效响应可能已计费，所有尝试都会记录；空搜索结果是有效结果，不会因此切换。每分钟和每日调用上限包含备用请求。</p><div class="form-grid">${field('主服务超时（秒）','search.timeout_seconds','','number','min="2" max="30" required')}${field('整次搜索总超时（秒）','search.total_timeout_seconds','包含主服务与备用服务；取消任务后不会再切换。','number','min="2" max="90" required')}</div>${[1,2].map(i=>{const path='search.fallbacks.fallback_'+i;return `<section class="media-section"><h3>备用搜索服务 ${i}</h3>${toggle('启用此备用服务',path+'.enabled','仍使用 SearXNG JSON 接口，不能填写其他类型服务地址。')}<div class="form-grid">${field('备用地址',path+'.base_url','需要与你的其他已启用搜索地址不同。','url','required')}${field('独立请求费用（元）',path+'.price_cny_per_request','未知请留空，不会沿用主服务价格。','number','min="0" max="100" step="any" data-nullable="true"')}${field('备用超时（秒）',path+'.timeout_seconds','','number','min="2" max="30" required')}</div></section>`;}).join('')}</details>`;
}
export function searchProvidersPanel(){return '<section class="panel media-section"><h2>搜索服务状态</h2><p class="note">状态只对应当前已保存配置的最近请求。单独测试不会切换到其他服务，便于定位具体地址。失败或取消可能已计费。</p><div id="search-provider-list"></div><h3>最近请求尝试</h3><div id="search-attempts"></div></section>';}
export async function bindSearchProviders(ctx,lifecycle){
 const {api,esc,saveEditor,confirmAction,notice}=ctx;
 const date=value=>new Date(value*1000).toLocaleString('zh-CN');
 async function refresh(){
  const data=await api('/api/search/providers');if(!lifecycle.active())return;
  document.querySelector('#search-provider-list').innerHTML=data.items.map(item=>`<article class="release"><h3>${labels[item.id]}</h3><p class="row-detail">${esc(item.base_url)}</p><p>${esc(providerStatusText(item,labels[item.id]))}</p><button type="button" class="button outline small" data-search-test="${item.id}">保存并测试此服务</button></article>`).join('');
  document.querySelector('#search-attempts').innerHTML=data.attempts.length?data.attempts.map(item=>`<div class="row"><div><strong>${labels[item.provider]||'历史搜索服务'}</strong><p class="row-detail">${esc(date(item.created_at))} · ${esc(item.status)}${item.error_kind?' · '+esc(item.error_kind):''}</p></div><span>${item.cost_cny==null?'费用未知':esc(item.cost_cny)+' 元'}</span></div>`).join(''):'<p class="empty-state">尚无搜索请求。</p>';
 }
 document.querySelector('#search-provider-list').onclick=e=>{
  const button=e.target.closest('[data-search-test]');if(!button||lifecycle.busy)return;
  const id=button.dataset.searchTest;
  confirmAction('测试'+labels[id]+'？','将先保存配置，再只向此地址发送固定公开测试词。可能产生一次请求费用，不会尝试其他服务。',async()=>{
   try{await lifecycle.run(async signal=>{if(!await saveEditor()||!lifecycle.active())return;await api('/api/search/providers/'+id+'/test',{method:'POST',body:JSON.stringify({confirm:true}),signal});if(lifecycle.active())notice('此搜索服务已返回有效结果。');});}
   finally{if(lifecycle.active())await refresh();}
  });
 };
 await refresh();return refresh;
}

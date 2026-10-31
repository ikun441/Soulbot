const $=s=>document.querySelector(s);
export function bindQuickActions(ctx){
 const {openDialog,closeDialog,send,api,guarded,esc,confirmAction,health}=ctx;
 const cards={
  version:{title:'版本与运行',description:'查看已发布版本、系统状态和升级说明。',links:[['version','版本记录'],['system','备份与恢复']]},
  qq:{title:'QQ 连接',description:health.qq_connected?'上次检查：QQ 已连接。可再次读取真实平台状态。':'上次检查：QQ 未连接。先检查 NapCat 登录和接收地址。',links:[['qq','连接设置']],action:'qq'},
  usage:{title:'Token 与费用',description:'查看实际调用、未知费用和预警记录。预算只预警，不暂停调用。',links:[['usage','查看调用与预警'],['behavior','修改预警阈值']]},
  settings:{title:'行动与权限',description:'行动前局部和全局记忆检索、后台整理和工具权限分别控制。',links:[['settings','打开设置'],['tools','工具权限'],['proactive','主动任务']]},
  memory:{title:'记忆依据',description:'审核记忆候选、查看作用域，或调整默认行动前检索。',links:[['memory','记忆管理'],['settings','检索与整理设置']],action:'memory'},
  emoji:{title:'表情与分类',description:'添加分类，浏览本地表情，或生成模板表情。',links:[['emoji','分类与本地库'],['meme','表情包工坊']],action:'emoji'},
  diary:{title:'日记与摘要',description:'查看基于确认活动的日记，事实不足时允许跳过。',links:[['diary','打开日记'],['qzone','空间草稿']],action:'diary'},
  model:{title:'模型与生图',description:'配置模型和能力，或进入本地 ComfyUI 生图页面。生成测试可能计费。',links:[['model','模型配置与试聊'],['image','图片生成'],['personality','编辑人格']]},
 };
 const tiles=[['memory','记忆依据','查看候选与检索记录'],['emoji','表情分类','挑选和管理本地表情'],['diary','日记与空间','查看真实活动与草稿'],['model','模型与生图','配置、试聊和图片生成']];
 document.querySelector('main .hero')?.insertAdjacentHTML('afterend',`<section class="quick-section"><div class="section-title"><h2>常用操作</h2><span class="muted">点击卡片展开快捷入口</span></div><div class="quick-grid">${tiles.map(([id,title,note])=>`<button type="button" class="panel quick-card" data-quick="${id}" aria-haspopup="dialog"><span class="quick-card-title">${title}<span aria-hidden="true">↗</span></span><span class="row-detail">${note}</span></button>`).join('')}</div></section>`);
 const dialog=$('#quick-dialog');let sequence=0;
 $('#quick-close').onclick=()=>{sequence++;closeDialog(dialog);};dialog.oncancel=e=>{e.preventDefault();sequence++;closeDialog(dialog);};
 document.querySelectorAll('main [data-quick]').forEach(button=>button.addEventListener('click',()=>{
  const card=cards[button.dataset.quick];if(!card)return;const ticket=++sequence;
  $('#quick-title').textContent=card.title;$('#quick-description').textContent=card.description;$('#quick-result').hidden=true;
  $('#quick-options').innerHTML=card.links.map(([target,label])=>`<a class="button outline" href="#${target}">${esc(label)}</a>`).join('')+(card.action?'<button type="button" class="button primary" id="quick-read">'+(card.action==='qq'?'检测实际连接':'读取当前状态')+'</button>':'')+(card.action==='qq'?'<button type="button" class="button quiet" id="quick-send-test">发送测试消息</button>':'');
  $('#quick-options').querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{sequence++;closeDialog(dialog);}));
  const read=$('#quick-read');if(read)read.onclick=e=>guarded(async()=>{
   const result=card.action==='qq'?await send('/api/qq/test','POST'):await api(card.action==='memory'?'/api/settings/memory':card.action==='emoji'?'/api/emoji/categories':'/api/diaries');
   if(ticket!==sequence||!dialog.open)return;$('#quick-result').hidden=false;$('#quick-result').textContent=card.action==='qq'?result.message:card.action==='memory'?`记忆总开关${result.memory_enabled?'开启':'关闭'}；最近检索局部 ${result.last.local_count||0} 条，全局 ${result.last.global_count||0} 条。`:`当前共 ${result.total} ${card.action==='emoji'?'个分类':'篇日记'}。`;
  },e.currentTarget);
  const sendTest=$('#quick-send-test');if(sendTest)sendTest.onclick=()=>{sequence++;closeDialog(dialog);confirmAction('发送测试消息给主人？','将向配置的主人 QQ 发送一条真实测试消息，每分钟最多一次。',async()=>{await send('/api/qq/send-test','POST',{confirm:true});});};
  openDialog(dialog);
 }));
}

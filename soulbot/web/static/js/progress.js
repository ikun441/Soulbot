// Shared release capabilities. Availability is reported separately by live health checks.
export const milestones = [
 {id:'M0',name:'基础与管理',state:'已实现',href:'#setup',detail:'虚拟环境启动、首次向导、配置校验、原生 WebUI 与 SQLite。'},
 {id:'M1',name:'QQ 与 AI',state:'已实现',href:'#qq',detail:'NapCat 接入、群私聊、消息过滤、频率设置、工具调用和 LLM 备用模型。'},
 {id:'M2',name:'多模态',state:'已实现',href:'#meme',detail:'Emoji 分类、meme 与扩展模板、TTS/ASR、ComfyUI、生图、卡片及 QQ 图片理解。'},
 {id:'M3',name:'人格与记忆',state:'已实现',href:'#relationships',detail:'可编辑人格、全局情绪与好感关系、身份名录、统一记忆和模型记忆写入更新。'},
 {id:'M4',name:'自主行为',state:'已实现',href:'#proactive',detail:'授权主动联系、媒体草稿、群聊参与、日记周摘要、公开搜索与自主探索，支持搜索备用服务。'},
 {id:'M5',name:'QQ 空间',state:'已实现',href:'#qzone',detail:'文字与配图草稿、可选审批、自主发布、可见范围、持久频率及平台能力检测。'},
 {id:'M6',name:'稳定性与发布',state:'持续完善',href:'#system',detail:'已有备份恢复、迁移、故障处理和自动回归；CI 启用、跨平台与长期运行仍待验收。'},
];
export const pendingWork = [
 '视频与通用文件理解；加好友、入退群等高影响社交工具。',
 'TTS、ASR 和生图的多 Provider 故障切换；非原生 Tool Calling 兼容层。',
 '向量记忆、完整矛盾合并和遗忘；探索资料到知识、话题与社交的自动闭环。',
 '插件在线升级与回滚、操作系统级隔离；第三方 GPU/模型环境安装和通用服务守护。',
 'GitHub CI 正式启用、跨平台及长期稳定性验收、WebUI 一键更新回滚。',
];
export function milestoneSummary(){
 return `<div class="roadmap"><strong>当前已提供的能力</strong><p>点击能力可直接打开对应页面。</p>${milestones.map(item=>`<a class="row" href="${item.href}"><span>${item.id} · ${item.name}</span><span class="badge ${item.state==='已实现'?'good':'wait'}">${item.state}</span></a>`).join('')}<p>功能已实现不等于当前服务已连接；可用状态请查看健康检查与各服务的实际测试结果。</p></div>`;
}
export function developmentProgress(){
 return `<section class="panel media-section"><h2>开发进度</h2><p class="note">M0–M5 的核心功能已实现，M6 持续完善。这里说明软件能力；真实 QQ、外部服务和不同操作系统的验收范围单独记录，不能由功能列表推断。</p><div class="rows">${milestones.map(item=>`<a class="row" href="${item.href}"><div><div class="row-title">${item.id} · ${item.name}</div><p class="row-detail">${item.detail}</p></div><span class="badge ${item.state==='已实现'?'good':'wait'}">${item.state}</span></a>`).join('')}</div><h3>尚未完成</h3><ul class="roadmap">${pendingWork.map(item=>`<li>${item}</li>`).join('')}</ul><p class="note">主动私聊继续要求收件人授权和审批；空间审批可选。好感与关系按机器人 QQ 与用户 QQ 全局共享，群名片仅用于对应群内的身份展示。预算只预警。</p></section>`;
}

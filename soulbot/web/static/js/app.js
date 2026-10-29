import { changePage, revealPage, moveNav, toast, openDialog, closeDialog, stopMotion, wallpaperFade } from "./motion.js";
import {imagePage, stopImageUI} from './image.js';
import {asrPage, stopASRUI} from './asr.js';
import {speechPage, stopSpeechUI} from './speech.js';
import {emojiPage, memePage, renderPage, toolsPage, stopMediaUI} from './media.js';
import {providerErrorText, providerStatusText} from './provider-guidance.js';
import {emotionPage, relationshipsPage, stopSocialUI} from './social.js';
import {memoryPage, stopMemoryUI} from './memory.js';
import {proactivePage, stopProactiveUI} from './proactive.js';
import {diaryPage, stopDiaryUI} from './diary.js';
import {qzonePage, stopQZoneUI} from './qzone.js';
import {searchPage, systemPage, bindPersonality, stopCompletionUI} from './completion.js';
const $ = (selector, node = document) => node.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 dashboard:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
 setup:'M8 3h8l4 5v13H4V3z M8 12h8 M8 16h5',
 qq:'M21 11a9 9 0 0 1-9 9H5l-3 2 1-6a9 9 0 1 1 18-5Z M8 10h.1 M12 10h.1 M16 10h.1',
 model:'m12 2 9 5v10l-9 5-9-5V7z M3 7l9 5 9-5 M12 12v10',
 personality:'M20 21v-2a7 7 0 0 0-14 0v2 M17 7a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z',
 appearance:'M12 3C6 3 2 7 2 12a10 10 0 0 0 10 10c3 0 1-5 4-5h3c5 0 4-14-7-14Z M7 9h.1 M12 6h.1 M17 9h.1',
 logs:'M5 3h14v18H5z M8 8h8 M8 12h8 M8 16h5',
 version:'M12 16v-5 M12 7h.1 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
 arrow:'M5 12h14 M13 6l6 6-6 6', shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z M8 12l3 3 5-6',
 heart:'M20 4c-3-2-6 0-8 3-2-3-5-5-8-3-7 5 8 16 8 16S27 9 20 4Z', clock:'M12 7v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z'
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.heart}"/></svg>`;
paths.behavior=paths.shield; paths.usage=paths.clock;paths.emoji=paths.heart;paths.meme=paths.model;paths.tools=paths.shield;paths.account=paths.shield;paths.cards=paths.setup;paths.emotion=paths.heart;paths.relationships=paths.personality;
const nav = [['dashboard','空间概览'],['setup','初次相遇','引导'],['qq','QQ 连接'],['model','AI 模型'],['personality','人格设定'],['emotion','全局情绪'],['relationships','好感与关系'],['memory','长期记忆'],['proactive','主动联系与调度'],['diary','本地日记'],['qzone','QQ 空间'],['behavior','聊天与权限'],['emoji','Emoji 表情库'],['meme','表情包工坊'],['cards','卡片工坊'],['tts','语音工坊'],['asr','语音识别'],['image','图片生成'],['search','搜索与探索'],['system','系统与备份'],['tools','工具权限'],['usage','Token 与费用'],['appearance','外观与壁纸'],['account','登录账号'],['logs','操作记录'],['version','关于 SoulBot']];
const steps = ['欢迎','管理员','连接 QQ','AI 模型','人格','多模态','行为权限','检查完成'];
let B, C, S, health, step = 0, page = '', noticeTimer, dirty = false, restartToken = false;
let chatController=null, webHistory=[];

async function api(url, options = {}) {
 const headers = {'X-Soulbot-Request':'1', ...options.headers};
 if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
 const response = await fetch(url, {...options, headers, credentials:'same-origin'});
 const data = await response.json();
 if (!response.ok) {
  if (response.status === 401 && B?.authenticated) { B.authenticated = false; renderLogin(); }
  const error = new Error(data.detail || '操作失败，请稍后重试');
  error.kind = data.kind;
  error.uncertain = Boolean(data.uncertain);
  error.status = response.status;
  throw error;
 }
 return data;
}
const send = (url, method, body) => api(url, {method, ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
function notice(message, error = false) {
 const box = $('#notice'); box.textContent = message; box.className = 'notice' + (error ? ' error' : ''); box.hidden = false;
 clearTimeout(noticeTimer); toast(box, true); noticeTimer = setTimeout(() => toast(box, false, () => {box.hidden = true;}), error ? 10000 : 6000);
}
async function guarded(task, button) {
 if (button) {button.disabled = true;button.setAttribute('aria-busy','true');}
 try {await task();} catch(error) {notice(error.message, true);} finally {if (button) {button.disabled = false;button.removeAttribute('aria-busy');}}
}
function heading(kicker, title, description) { return `<div class="intro"><div class="eyebrow">${kicker}</div><h1>${title}</h1><p class="description">${description}</p></div>`; }
function orb() {return '<div class="orb-scene" aria-hidden="true"><div class="orb">♡</div><div class="orbit"></div><span class="spark s1">✧</span><span class="spark s2">✧</span><div class="orb-caption">A SOUL OF YOUR OWN</div></div>';}
function badge(text, kind = '') {return `<span class="badge ${kind}">${esc(text)}</span>`;}
function get(path) { return path.split('.').reduce((o,k) => o?.[k], C); }
function set(path, value) {const keys = path.split('.'); const last = keys.pop(); keys.reduce((o,k) => o[k], C)[last] = value;}
function field(label, path, help = '', type = 'text', extra = '', full = false) {
 return `<div class="field ${full ? 'full' : ''}"><label for="${path}">${label}</label><input id="${path}" data-field="${path}" type="${type}" value="${esc(get(path))}" ${extra}>${help ? `<small>${help}</small>` : ''}</div>`;
}
function textarea(label, path, help = '', max = 2000) {return `<div class="field full"><label for="${path}">${label}</label><textarea id="${path}" data-field="${path}" maxlength="${max}">${esc(get(path))}</textarea><small>${help}</small></div>`;}
function secret(label, name, help) {return `<div class="field"><label for="${name}">${label} ${badge(S[name] ? '已配置' : '未配置')}</label><input type="password" id="${name}" data-secret="${name}" maxlength="16384" autocomplete="new-password" placeholder="留空保留已有密钥"><small>${help}</small></div>`;}
function select(label, path, options, help = '') {return `<div class="field"><label for="${path}">${label}</label><select id="${path}" data-field="${path}">${options.map(([value, text]) => `<option value="${value}" ${get(path) === value ? 'selected' : ''}>${text}</option>`).join('')}</select><small>${help}</small></div>`;}
function toggle(label, path, help) {return `<label class="switch-row"><span><strong>${label}</strong><span class="row-detail">${help}</span></span><input type="checkbox" data-field="${path}" ${get(path) ? 'checked' : ''}></label>`;}
function collect() {
 const form = $('#editor'); if (form && !form.reportValidity()) return false;
 document.querySelectorAll('[data-field]').forEach(input => {set(input.dataset.field, input.dataset.list ? input.value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean) : input.type === 'checkbox' ? input.checked : input.type === 'number' ? (input.dataset.nullable && input.value === '' ? null : Number(input.value)) : input.value);});
 return true;
}
async function saveEditor() {
 if (!collect()) return false;
 const changes = {};
 document.querySelectorAll('[data-secret]').forEach(input => {if (input.value) changes[input.dataset.secret] = input.value;});
 if (Object.keys(changes).length) {
  const r = await send('/api/secrets','PUT',changes); S = r.secrets; restartToken ||= r.restart_required;
  document.querySelectorAll('[data-secret]').forEach(input => {input.value = '';});
 }
 const result = await send('/api/config','PUT',C); C = result.config; S = result.secrets;
 const receiver = $('#reverse-ws-effective');if(receiver)receiver.textContent=result.qq_reverse_ws_url;
 dirty = false; applyAppearance();
 notice((result.restart_required || restartToken) ? '配置已保存。QQ、端口或 Token 更改需停止并重新启动 SoulBot。' : '配置已保存，立即生效。');
 return true;
}
function applyAppearance() {
 const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
 const motion = C?.appearance.motion && !reduced;
 document.body.dataset.motion = String(Boolean(motion));
 if (!motion) stopMotion();
 const wallpaper = $('#wallpaper');
 if (!C?.appearance.wallpaper) {wallpaper.replaceChildren();delete wallpaper.dataset.url;return;}
 const existing=wallpaper.lastElementChild;
 if(wallpaper.dataset.url===C.appearance.wallpaper && existing){
  if(existing.tagName==='VIDEO'){if(motion)existing.play().catch(()=>{});else existing.pause();return;}
  if(!C.appearance.wallpaper.endsWith('.gif'))return;
 }
 const previous=[...wallpaper.children];wallpaper.dataset.url=C.appearance.wallpaper;

 if (C.appearance.wallpaper_kind === 'video') {
  const video = document.createElement('video');video.src = C.appearance.wallpaper;video.muted = true;video.loop = true;video.playsInline = true;video.autoplay = Boolean(motion);video.setAttribute('aria-hidden','true');wallpaper.append(video);wallpaperFade(video,previous);
  if (motion) video.play().catch(() => {});
 } else if (motion || !C.appearance.wallpaper.endsWith('.gif')) {
  const img = document.createElement('img');img.src = C.appearance.wallpaper;img.alt = '';wallpaper.append(img);wallpaperFade(img,previous);
 } else {wallpaper.replaceChildren();delete wallpaper.dataset.url;}
}
function updateNav() {
 $('#navigation').innerHTML = nav.map(([id,label,tip]) => `<a class="nav-link ${page === id ? 'active' : ''}" href="#${id}" ${page===id ? 'aria-current="page"' : ''}>${icon(id)}<span>${label}</span>${tip ? `<span class="nav-tip">${tip}</span>` : ''}</a>`).join('');
 $('#breadcrumb').textContent = nav.find(x => x[0] === page)?.[1] || '欢迎';
 $('#logout').hidden = !B?.authenticated; moveNav($('#navigation'));
}
function mount(html) {
 stopMediaUI();stopSpeechUI();stopASRUI();stopImageUI();stopSocialUI();stopMemoryUI();stopProactiveUI();stopDiaryUI();stopQZoneUI();stopCompletionUI();
 chatController?.abort();chatController=null;
 $('#main').innerHTML = html; updateNav(); revealPage($('#main'));
 window.scrollTo({top:0,behavior:'instant'});
 $('#main').querySelectorAll('input,textarea,select').forEach(input => input.addEventListener('input', () => {if(!input.closest('#chat-form')&&!input.closest('[data-transient]'))dirty = true;}));
 const editor = $('#editor'); if (editor) editor.addEventListener('submit', e => {e.preventDefault(); guarded(saveEditor, $('#save'));});
}
function renderLogin() {
 page = 'login'; mount(`${heading('WELCOME BACK','欢迎回到你的小小宇宙。','登录管理员账户，继续照顾 SoulBot 的每一个细节。')}<form class="panel auth-panel" id="login-form"><h2>管理员登录</h2><div class="field"><label for="username">用户名</label><input id="username" name="username" required autocomplete="username" maxlength="40"></div><div class="field"><label for="password">密码</label><input id="password" name="password" type="password" required autocomplete="current-password" maxlength="128"></div><div class="actions"><button class="button primary" type="submit">进入我的空间 ${icon('arrow')}</button></div><p class="note">管理会话有效期为 12 小时。忘记密码请参考 README.md；请勿把 data 目录分享给他人。</p></form>`);
 $('#login-form').onsubmit = e => {e.preventDefault();guarded(async () => {await send('/api/auth/login','POST',{username:$('#username').value,password:$('#password').value});$('#password').value='';await refresh();dirty=false;render(B.initialized ? 'dashboard' : 'setup');}, $('button[type=submit]'));};
}
async function refresh() {
 B = await api('/api/bootstrap');
 $('#side-version').textContent = `${B.version} · 本地运行`; $('#footer-version').textContent = B.version;
 if (B.authenticated) { const config = await api('/api/config');C = config.config;S = config.secrets;health = await api('/api/health');step = B.wizard_step;applyAppearance(); }
}
function dashboard() {
 const metric = (label, value, note, glyph) => `<article class="panel metric"><div class="metric-head">${label}<span class="metric-icon">${icon(glyph)}</span></div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></article>`;
 const row = (glyph,title,detail,status,kind='') => `<div class="row"><div class="row-left"><span class="feature-icon">${icon(glyph)}</span><div><div class="row-title">${title}</div><div class="row-detail">${detail}</div></div></div>${badge(status,kind)}</div>`;
 mount(`${heading('OVERVIEW','一处空间，一份独特的陪伴。','从人格到每一次表达，慢慢塑造属于你的 SoulBot。')}<section class="panel hero"><div class="hero-copy"><div class="eyebrow">HELLO, ${esc(C.soulbot.name.toUpperCase())}</div><h2>让陪伴，有自己的模样。</h2><p>为它取个名字，写下你期待的性格。<br>从一次简单的配置，开始一段新的日常。</p><div class="actions"><a href="#setup" class="button primary">${B.initialized ? '重新查看向导' : '继续初次配置'} ${icon('arrow')}</a><a href="#personality" class="button outline">编辑人格</a></div></div>${orb()}</section><div class="section-title"><h2>此刻的运行状态</h2><span class="muted">来自本机的真实状态</span></div><div class="metric-grid">${metric('系统版本',B.version,'周期日记、搜索与稳定部署','model')}${metric('QQ 连接',health.qq_connected ? '已连接' : '未连接',health.qq_enabled ? '已启用适配器' : '默认关闭，等待配置','qq')}${metric('今日 Token',String(health.usage?.reserved_or_used_tokens ?? 0),'包含预留与保守估算用量','usage')}${metric('安全边界',C.proactive.enabled?'主动私聊已启用':'主动私聊关闭','媒体工具限次执行，发送前复核权限','shield')}</div><div class="section-title"><h2>让每一步，都清晰可见</h2><a href="#version" class="tag">查看开发进度 ↗</a></div><div class="two-columns"><section class="panel"><h2>连接与服务</h2><p class="muted">先建立可靠的连接，再让故事发生。</p><div class="rows">${row('model','Web 管理服务','FastAPI · 原生浏览器界面','运行中','good')}${row('logs','本地数据库','SQLite · 配置与状态安全保存',health.sqlite ? '正常' : '异常',health.sqlite?'good':'wait')}${row('qq','NoneBot2 / OneBot V11',health.nonebot ? '驱动与适配器已初始化' : '仅管理服务模式',health.nonebot ? '已就绪' : '未加载',health.nonebot?'good':'wait')}${row('heart','AI 模型调用',health.llm,health.llm_provider_test?.state==='available'?'最近调用成功':health.llm_provider_test?.state==='failed'?'最近调用失败':health.llm_provider_test?.state==='unconfigured'?'未配置':'待实际测试',health.llm_provider_test?.state==='available'?'good':'wait')}</div><div class="actions"><button class="button quiet small" id="health-refresh">刷新健康检查</button><a href="#qq" class="button quiet small">管理 QQ 连接</a></div></section><section class="panel"><div class="mini-label">A GENTLE REMINDER</div><h2 style="margin-top:14px">温柔，也需要边界。</h2><p class="muted">人格与情绪是可配置的程序模拟。所有主动行为都应建立在真实事件和用户授权之上。</p><div class="note">默认被动回应 · 私聊与群聊隔离<br>自主行为按配置执行 · 空间审批可选<br>费用预警阈值 ${esc(C.behavior.daily_cost_cny)} 元 / 天（价格未知时费用会标记为未知）</div><div class="roadmap"><strong>正在生长的能力</strong><br>M1 QQ 与 AI → M2 多模态<br>M3 人格记忆 → M4 自主行为<br>M5 空间草稿 → M6 稳定发布</div></section></div>`);
 const llmRow=[...$('#main').querySelectorAll('.row')].find(item=>item.querySelector('.row-title')?.textContent==='AI 模型调用');
 const llmDetail=document.createElement('div');llmDetail.className='row-detail';llmDetail.textContent=providerStatusText(health.llm_provider_test,'LLM');llmRow?.querySelector('.row-left > div')?.append(llmDetail);
 const emotion=health.emotion;if(emotion)llmRow?.parentElement.insertAdjacentHTML('beforeend',row('heart','全局模拟情绪',emotion.enabled?(emotion.automatic?'随成功互动与时间变化':'当前手动冻结'):'已关闭，数值保留',emotion.state_label,emotion.enabled?'good':'wait'));
 $('#health-refresh').onclick = e => guarded(async () => {health = await api('/api/health');dashboard();notice('健康状态已刷新');},e.currentTarget);
}
function qqForm() {
 const host=C.web.host==='0.0.0.0'?'127.0.0.1':C.web.host;const automatic=`ws://${host.includes(':')?'['+host+']':host}:${C.web.port}/onebot/v11/ws`;
 return `<div class="note">推荐 <strong>NapCat + OneBot V11 反向 WebSocket</strong>。NapCat 负责登录 QQ，SoulBot 接收事件。两者需要分别启动。<a href="https://napneko.github.io/guide/napcat" target="_blank" rel="noopener noreferrer">官方安装指南 ↗</a></div><div class="connection-flow"><b>QQ / NapCat</b><span>→</span><b>OneBot V11</b><span>→</span><b>SoulBot</b></div><div class="note">在 NapCat 的 WebSocket 客户端中填写 <code id="reverse-ws-effective">${esc(C.qq.reverse_ws_url||automatic)}</code>，配置 Token 时双方填写相同值；仅本机连接允许双方留空。<br>跨机器连接时使用 SoulBot 实际 IP 或反代域名，并配置监听地址与代理的 WebSocket 转发。</div><div class="form-grid">${select('连接方式','qq.connection',[['reverse','反向 WebSocket（推荐）'],['forward','正向 WebSocket（高级）']],'正向模式由 SoulBot 连接 NapCat 服务端。')}${field('反向 / 反代 WS 地址','qq.reverse_ws_url','保存到 .env；留空按监听配置自动生成。支持 ws:// 或 wss://，自定义接收路径重启生效。地址不会自动修改监听端口或启用 QQ。','url',`maxlength="2048" placeholder="${esc(automatic)}"`)}${field('本机测试 / 正向连接地址','qq.test_url','此地址是否有效需检测，不能把反向接收端当成 NapCat API。','url','required')}${field('SoulBot 监听端口','web.port','反向 WS 与 WebUI 共用此端口。NapCat 连接 2525 时此处也应为 2525；修改后重启并用新端口打开 WebUI。','number','min=1024 max=65535 required')}${secret('Access Token','onebot_access_token','仅本机连接可留空；监听所有地址或连接远程服务时必须配置。留空保留已有值。')}</div>${toggle('启用 QQ 适配器','qq.enabled','本机允许无 Token；跨机器接入必须保存 Token。修改接入参数后需重启。连接后按群聊策略处理文本消息。')}<div class="actions"><button type="button" class="button outline" id="test-qq">保存并检测 QQ 连接</button><button type="button" class="button quiet" id="send-qq-test">向主人发送测试消息</button></div><p id="qq-result" class="note" hidden></p>`;
}

async function accountPage() {
 const account=await api('/api/auth/account');if(page!=='account')return;
 const disabled=account.managed_by_process?'disabled':'';
 mount(`${heading('YOUR ACCOUNT','登录，也由你掌握。','账号保存在项目 .env，修改后立即生效。')}<form id="account-form" class="panel auth-panel"><h2>WebUI 管理员</h2><p class="note">${account.managed_by_process?'当前账号由进程环境变量管理，请修改部署环境后重启。':'密码以随机盐和 PBKDF2 哈希保存。修改账号会退出其他登录设备，当前页面保持登录。'}</p><div class="field"><label for="account-user">用户名</label><input id="account-user" value="${esc(account.username)}" required pattern="[a-zA-Z0-9_\\-]{3,40}" minlength="3" maxlength="40" autocomplete="username" ${disabled}></div><div class="field"><label for="account-current">当前密码</label><input id="account-current" type="password" required maxlength="128" autocomplete="current-password" ${disabled}></div><div class="field"><label for="account-password">新密码</label><input id="account-password" type="password" minlength="12" maxlength="128" autocomplete="new-password" ${disabled}><small>至少 12 位；留空保留当前密码。</small></div><div class="field"><label for="account-confirm">确认新密码</label><input id="account-confirm" type="password" minlength="12" maxlength="128" autocomplete="new-password" ${disabled}></div><div class="actions"><button class="button primary" type="submit" ${disabled}>保存账号</button></div></form>`);
 $('#account-form').onsubmit=e=>{e.preventDefault();guarded(async()=>{
  const password=$('#account-password'), confirmation=$('#account-confirm');
  if(password.value!==confirmation.value){confirmation.setCustomValidity('两次密码不一致');confirmation.reportValidity();confirmation.oninput=()=>confirmation.setCustomValidity('');return;}
  confirmation.setCustomValidity('');
  const result=await send('/api/auth/account','PUT',{username:$('#account-user').value,current_password:$('#account-current').value,password:password.value||null});
  $('#account-current').value='';password.value='';confirmation.value='';dirty=false;notice(result.message);
 },$('#account-form button[type=submit]'));};
}
function modelForm() {
 return `<div class="note">兼容 Chat Completions 接口。保存后可以读取服务商实际模型列表、发送连接测试或试聊；有生成的请求都会计入预算。</div><div class="form-grid">${field('API Base URL','llm.base_url','通常以 /v1 结尾，以服务商文档为准。','url','required')}${field('模型名称','llm.model','手动填写或先读取真实模型列表。','text','maxlength="120" list="model-ids" placeholder="服务商模型 ID"')}<datalist id="model-ids"></datalist>${secret('API Key','llm_api_key','独立保存，不会回显。留空保留已有值。')}</div>${toggle('启用主模型','llm.enabled','关闭后可使用已配置的备用模型。')}<div class="actions"><button type="button" class="button outline" id="model-list">保存并读取模型列表</button><button type="button" class="button outline" id="model-test">保存并测试连接</button><button type="button" class="button outline" id="model-tools-test">验证并启用工具调用</button></div><p class="note" id="model-result" hidden></p><details class="advanced"><summary>高级兼容性、价格与故障切换</summary><div class="form-grid" style="margin-top:18px">${field('请求超时（秒）','llm.timeout_seconds','','number','min="1" max="120" required')}${field('临时错误重试次数','llm.retries','无效密钥和参数错误不在同一服务重复重试。','number','min="0" max="2" required')}${field('上下文上限','llm.context_window','按服务商实际模型限制填写，当前不会猜测模型能力。','number','min="1024" max="2000000" required')}${select('输出上限参数','llm.max_output_field',[['max_tokens','max_tokens（常见兼容接口）'],['max_completion_tokens','max_completion_tokens']])}${field('输入价格（元 / 百万 Token）','llm.input_price_cny_per_million','输入和输出价格同时留空表示未知。','number','min="0" max="100000" step="any" data-nullable="true"')}${field('输出价格（元 / 百万 Token）','llm.output_price_cny_per_million','请使用你的实际人民币计价；0 仅用于确认免费的服务。','number','min="0" max="100000" step="any" data-nullable="true"')}</div>${toggle('使用服务商流式接口','llm.streaming','服务商必须支持 stream=true；QQ 会在生成完成后按句分段发送。')}${toggle('请求流式用量','llm.stream_usage','仅在服务商支持 stream_options.include_usage 时开启。')}${toggle('声明原生工具调用能力','llm.supports_tools','开启后 QQ 可按权限使用生图、表情和其他工具；可使用上方按钮实际验证并启用。')}<div class="note">缺少真实用量时使用保守预留值；价格未知时不会显示为免费，并显示预警。</div><div id="fallbacks">${fallbackForms()}</div></details>`;
}
function fallbackForms(){
 C.llm.fallbacks ||= {};
 return ['fallback_1','fallback_2'].map((name,index)=>{
  C.llm.fallbacks[name] ||= {...C.llm,enabled:false,model:'',input_price_cny_per_million:null,output_price_cny_per_million:null,fallbacks:undefined,streaming:false,supports_tools:false,stream_usage:false,max_output_field:'max_tokens'};delete C.llm.fallbacks[name].fallbacks;
  const path=`llm.fallbacks.${name}`;
  return `<section class="fallback-section"><h3>备用服务 ${index+1}</h3>${toggle('启用此备用服务',`${path}.enabled`,'按顺序在主服务不可用时尝试，仅使用声明支持的能力。')}<div class="form-grid">${field('备用 Base URL',`${path}.base_url`,'','url','required')}${field('备用模型 ID',`${path}.model`,'','text','maxlength="120"')}${secret('备用 API Key',`llm_${name}`,'独立密钥，不与主服务共享。')}${field('输入价格（元 / 百万 Token）',`${path}.input_price_cny_per_million`,'','number','min="0" step="any" data-nullable="true"')}${field('输出价格（元 / 百万 Token）',`${path}.output_price_cny_per_million`,'未知请留空；不会自动沿用主服务报价。','number','min="0" step="any" data-nullable="true"')}</div>${toggle('备用服务支持工具调用',`${path}.supports_tools`,'工具能力不同的服务不会被混用。')}</section>`;
 }).join('');
}
function personalityForm() {return `<div class="form-grid">${field('AI 名称','soulbot.name','给你的伙伴一个名字。','text','required maxlength="40"')}${field('对主人的称呼','personality.owner_title','','text','maxlength="40"')}${field('主人 QQ 号','personality.owner_qq','仅用于本地配置，不随默认脱敏配置导出。','text','pattern="[1-9][0-9]{4,11}" maxlength="12" placeholder="可稍后设置"')}${field('说话风格','personality.style','','text','maxlength="500"')}${textarea('角色描述','personality.description','人格、情绪和好感度用于当前对话；长期记忆可在独立页面启用并审核。')}${field('兴趣爱好','personality.interests','','text','maxlength="500"',true)}</div><details><summary>高级：自定义人格 Prompt</summary><div style="margin-top:16px">${textarea('人格 Prompt','personality.prompt','人格不能覆盖系统权限、预算或审批规则。',8000)}</div></details>`;}
function permissionsForm() {return `${toggle('长回复分段发送','behavior.split_long_replies','模型输出超过两句时默认每段最多两句，最多分为五条；每段遵守发送限速与权限。')}<div class="form-grid">${select('默认群聊模式','behavior.group_mode',[['silent','静默 · 仅管理员命令'],['mention_only','被动 · @、唤醒和有效连续对话'],['natural','自然社交 · 8% 候选概率'],['active','活跃社交 · 20% 候选概率']],'普通消息会先经过权限、概率和冷却，再考虑调用模型。')}${field('每日 Token 预警阈值','behavior.daily_tokens','每次调用持续记录用量，达到阈值仅预警，不停止调用。','number','min="1000" max="10000000" required')}${field('每日费用预警阈值（元）','behavior.daily_cost_cny','未知价格将预警，不当作免费，也不暂停调用。','number','min="0.01" max="10000" step="0.01" required')}${field('时区','soulbot.timezone','用于每日额度与调度时间，例如 Asia/Shanghai。','text','required')}</div><div class="form-grid">${field('预警触发比例','behavior.budget_warning_ratio','0.8 表示达到阈值的 80% 时提醒；超阈值继续调用。','number','min="0.1" max="1" step="0.05" required')}${field('单次输入 Token 预警','limits.max_input_per_request','仅预警，实际输入容量由模型上下文决定。','number','min="256" max="128000" required')}${field('单任务 Token 预警','limits.max_per_agent_task','仅预警；工具轮数和次数仍独立限制。','number','min="512" max="300000" required')}</div>${toggle('夜间免打扰','behavior.quiet_hours','00:00—08:00 暂停主动群聊，仍允许明确 @ 和私聊。')}<div class="note">自主探索：可在独立页面启用 · 主动私聊：用户授权及审批后可用<br>本地日记：可在独立页面配置 · QQ 空间：支持可选审批与自主发布</div>`;}
function editorPage(kind) {
 const titles = {qq:['CONNECTION','把 SoulBot 带到 QQ。','从 NapCat 开始，建立第一条连接。'],model:['AI PROVIDERS','选择它的思考方式。','密钥独立保存，服务商可以随时更换。'],personality:['PERSONALITY','每一份个性，都由你定义。','让名字、语气和兴趣，成为独特表达的起点。']};
 mount(`${heading(...titles[kind])}<form id="editor" class="panel">${kind==='qq'?qqForm():kind==='model'?modelForm():personalityForm()}<div class="actions"><button id="save" class="button primary" type="submit">保存配置</button><span class="save-status">每次保存会自动备份上一版</span></div></form>${kind==='model'?chatPanel():''}`);bindQQ();if(kind==='model')bindModel();if(kind==='personality'){ $('#main').insertAdjacentHTML('beforeend',`<section class="panel media-section" id="personality-extras"><h2>人格模板与文件</h2><label for="personality-template">基础模板</label><select id="personality-template"></select><div class="actions"><button class="button outline" id="personality-apply">应用模板</button><a class="button outline" href="/api/personality/export" download>导出人格 JSON</a><a class="button quiet" href="#model">使用已保存人格测试聊天</a></div><label for="personality-import">导入人格 JSON（保留主人设置）</label><input id="personality-import" type="file" accept=".json"><button class="button quiet" id="personality-preview">预览已保存的 Prompt</button><pre class="memory-text" id="personality-prompt"></pre></section>`);guarded(()=>bindPersonality(mediaContext()));}
}
function bindQQ() {const button=$('#test-qq');if(button) button.onclick=()=>guarded(async()=>{if(!await saveEditor())return;const result=await send('/api/qq/test','POST');$('#qq-result').hidden=false;$('#qq-result').textContent=result.message;},button);const outbound=$('#send-qq-test');if(outbound)outbound.onclick=()=>confirmAction('发送 QQ 测试消息？','这会向配置中的主人 QQ 号发送一条真实消息。每分钟最多一次。',async()=>{if(!await saveEditor())return;const result=await send('/api/qq/send-test','POST',{confirm:true});$('#qq-result').hidden=false;$('#qq-result').textContent=result.message;});}
function wizard() {
 if (!B.has_admin && step > 1) step = 0;
 let content;
 if (step===0) content=`<section class="panel hero"><div class="hero-copy"><div class="eyebrow">A NEW CHAPTER</div><h2>你好，初次见面。</h2><p>只需几步，为你的 AI 伙伴准备一个家。<br>设置管理账户，连接 QQ，再写下它的个性。</p><div class="tag">约 5 分钟 · 配置进度自动保留 · 可跳过选配功能</div></div>${orb()}</section><div class="three-columns" style="margin-top:18px"><div class="note">01 / 准备 NapCat<br>负责登录你的机器人 QQ。</div><div class="note">02 / 准备模型 API<br>需要一个兼容接口和 API Key。</div><div class="note">03 / 想一个名字<br>其余功能可以稍后开启。</div></div>`;
 if (step===1) content=B.has_admin?'<div class="note">管理员账户已创建，当前已登录。可以继续下一步。</div>':`<div class="form-grid"><div class="field full"><label for="admin-user">管理员用户名</label><input id="admin-user" required pattern="[a-zA-Z0-9_\\-]{3,40}" minlength="3" maxlength="40" autocomplete="username" placeholder="3—40 位字母、数字、下划线或短横线"><small>这是 WebUI 管理账户，与 QQ 账户无关。</small></div><div class="field"><label for="admin-pass">管理员密码</label><input type="password" id="admin-pass" required minlength="12" maxlength="128" autocomplete="new-password"><small>至少 12 位，请保存到密码管理器。</small></div><div class="field"><label for="admin-confirm">再次输入密码</label><input type="password" id="admin-confirm" required minlength="12" maxlength="128" autocomplete="new-password"></div></div><div class="note">密码使用独立随机盐和 PBKDF2 哈希保存，不会明文保存。默认仅本机可以访问管理界面。</div>`;
 if (step===2) content=qqForm();
 if (step===3) content=modelForm();
 if (step===4) content=personalityForm();
 if (step===5) content=`<div class="note">本版本已开放本地 Emoji、真实 meme-generator 和 HTML 卡片。完成向导后可进入各自工坊，准备素材/浏览器并生成图片。</div>${toggle('启用本地表情库','emoji.enabled','自动扫描项目 emoji 目录，支持中文分类和标签。')}${toggle('启用 HTML 卡片','html_render.enabled','先在卡片工坊准备项目专用 Chromium；不调用图片生成。')}<p class="muted">meme-generator 与 Chromium 均为选配，安装前会说明下载内容。语音工坊已支持 Edge 与兼容接口，可在完成向导后单独配置和试音。语音识别支持本地 Whisper 与独立兼容接口，可在语音识别页上传测试。图片生成接口可在图片生成页配置并实际测试；搜索继续开发，现在可以跳过。</p>`;
 if (step===6) content=permissionsForm();
 if (step===7) content=`<div class="note">这里只读取本机连接状态和各 Provider 最近一次真实测试结果，不会发起模型、语音、生图请求或 QQ 消息。需要测试时，请进入对应页面手动操作；云端测试可能计费。</div><div class="rows"><div class="row"><span>管理员账户</span>${badge(B.has_admin?'已创建':'未创建',B.has_admin?'good':'wait')}</div><div class="row"><span>QQ 适配器配置</span>${badge(C.qq.enabled?'已启用，可能需要重启':'暂未启用')}</div><div class="row"><span>主动私聊</span>${badge(C.proactive.enabled?'已启用':'关闭',C.proactive.enabled?'wait':'good')}</div><div class="row"><span>QQ 空间手动发布</span>${badge(C.qzone.enabled?(C.qzone.require_approval?'已启用，需逐条审批':'已启用，无需逐条审批'):'关闭',C.qzone.enabled?'wait':'good')}</div></div><div class="actions"><button type="button" id="final-check" class="button outline">读取接入验收状态</button><a href="#qq" class="button quiet small">QQ 设置</a><a href="#model" class="button quiet small">LLM 测试</a></div><div id="final-result" class="rows" aria-live="polite"><p class="note">尚未读取 Provider 状态。</p></div>`;
 mount(`${heading('FIRST HELLO','从这里，遇见你的 SoulBot。','每一步都附有说明。选配功能可以留到以后，已保存的进度不会丢失。')}<div class="wizard-steps" aria-label="配置步骤">${steps.map((name,i)=>`<span class="step-dot ${i===step?'current':i<step?'done':''}" ${i===step?'aria-current="step"':''}>${String(i+1).padStart(2,'0')} ${name}</span>`).join('')}</div><form id="editor" class="panel"><div class="wizard-title"><span class="step-number">${String(step+1).padStart(2,'0')}</span><div><div class="mini-label">LET’S MAKE IT YOURS</div><h2>${steps[step]}</h2></div></div><div class="wizard-body">${content}</div><div class="wizard-footer"><span class="save-status">${B.has_admin?'点击下一步保存配置':'先创建账户，再安全保存进度'}</span><div class="actions">${step>0?'<button type="button" id="previous" class="button quiet">上一步</button>':''}<button type="button" id="next" class="button primary">${step===0?'开始配置':step===5?'跳过，稍后配置':step===7?'完成并进入空间':'保存并继续'} ${icon('arrow')}</button></div></div></form>`);
 $('#next').onclick=e=>guarded(async()=>{
  if(!$('#editor').reportValidity()) return;
  if(step===1&&!B.has_admin){if($('#admin-pass').value!==$('#admin-confirm').value){$('#admin-confirm').setCustomValidity('两次密码不一致');$('#admin-confirm').reportValidity();$('#admin-confirm').oninput=()=>$('#admin-confirm').setCustomValidity('');return;}await send('/api/auth/setup','POST',{username:$('#admin-user').value,password:$('#admin-pass').value});await refresh();step=1;}
  if(step>=2&&!await saveEditor())return;
  if(step===7){await send('/api/wizard/complete','POST');B.initialized=true;dirty=false;changePage(()=>render('dashboard'));return;}
  step++;if(B.authenticated)await send('/api/wizard/progress','PUT',{step});dirty=false;changePage(wizard);
 },e.currentTarget);
 const previous=$('#previous');if(previous)previous.onclick=e=>guarded(async()=>{if(step>=2&&!await saveEditor())return;step--;if(B.authenticated)await send('/api/wizard/progress','PUT',{step});dirty=false;changePage(wizard,'backward');},e.currentTarget);
 const check=$('#final-check');if(check)check.onclick=e=>guarded(async()=>{const [nextHealth,providers]=await Promise.all([api('/api/health'),api('/api/providers/status')]);health=nextHealth;const labels={llm:['AI 模型','#model','仅在 AI 模型页面手动测试；可能产生费用。'],tts:['语音合成','#tts','生成试听会请求服务并可能计费。'],asr:['语音识别','#asr','云端会上传录音；本地识别在本机处理。'],image_generation:['图片生成','#image','生成会请求服务并可能计费。']};const rows=Object.entries(labels).map(([key,[name,href,note]])=>{const status=providers[key];return `<div class="row"><div class="row-left"><div><a class="row-title" href="${href}">${name} · ${esc(status.state)}</a><div class="row-detail">${esc(providerStatusText(status,name))}</div><div class="row-detail">${note}${status.last_test_at?' 最近测试：'+esc(status.last_test_at):''}</div></div></div></div>`;}).join('');const qq=`<div class="row"><div><span class="row-title">NapCat / QQ</span><div class="row-detail">${health.qq_connected?'NoneBot 当前报告已连接机器人；这不等同于发送测试消息。':health.nonebot?'适配器已加载，当前没有已连接机器人。':'NoneBot 适配器尚未加载。'} <a href="#qq">前往连接设置</a></div></div><span class="badge ${health.qq_connected?'good':'wait'}">${health.qq_connected?'已连接':'未连接'}</span></div>`;$('#final-result').innerHTML=`${rows}${qq}<p class="note">SQLite：${health.sqlite?'正常':'异常'} · NoneBot：${health.nonebot?'已加载':'未加载'} · Python ${esc(health.python)}。以上 Provider 状态只反映当前配置下最近一次真实请求；“已配置”不代表“可用”。</p>`;},e.currentTarget);
 bindQQ();if(step===3)bindModel();
}
function appearance() {
 mount(`${heading('MAKE IT YOURS','给小小宇宙，换一种心情。','粉色渐变与毛玻璃是起点，你也可以选择自己的风景。')}<form id="editor" class="panel"><h2>你的壁纸</h2><p class="muted">上传本地图片，或使用一段安静循环的视频。文件只保存在这台机器。</p><div class="file-zone"><label for="wallpaper-file">选择壁纸文件</label><input type="file" id="wallpaper-file" accept="image/png,image/jpeg,image/gif,image/webp,video/mp4,video/webm"><p class="row-detail">PNG / JPG / GIF / WebP / MP4 / WebM，最大 32MB。视频默认静音。</p></div>${toggle('开启动态效果','appearance.motion','允许柔和的渐变漂移和视频壁纸播放；尊重系统的“减少动态效果”设置。关闭时 GIF 壁纸回退为渐变。')}<div class="actions"><button id="save" class="button primary" type="submit">保存外观</button><button id="remove-wallpaper" class="button quiet" type="button">恢复渐变背景</button></div></form><section class="panel" style="margin-top:20px"><h2>配置与备份</h2><p class="muted">每次保存会备份上一版非敏感配置。导出不包含密钥与主人 QQ，配置中自填的文本仍请自行检查。</p><div class="actions"><a class="button outline" href="/api/config/export" download>导出脱敏配置</a><button class="button quiet" id="restore">恢复上一版</button></div><div class="note">恢复会覆盖当前非敏感配置。完整备份方法见 README.md；壁纸和密钥不随此导出。</div></section>`);
 $('#wallpaper-file').onchange=e=>guarded(async()=>{const file=e.target.files[0];if(!file)return;if(file.size>32*1024*1024)throw new Error('文件不能超过 32MB');const data=new FormData();data.append('file',file);const result=await api('/api/wallpaper',{method:'POST',body:data});C.appearance.wallpaper=result.url;C.appearance.wallpaper_kind=result.kind;await saveEditor();},$('#save'));
 $('#remove-wallpaper').onclick=e=>guarded(async()=>{C.appearance.wallpaper='';await saveEditor();},e.currentTarget);
 $('#restore').onclick=()=>confirmAction('恢复上一版配置？','当前非敏感配置将被上一版替换。密钥不会改变。',async()=>{const r=await send('/api/config/restore','POST');C=r.config;S=r.secrets;applyAppearance();dirty=false;notice(r.restart_required?'已恢复，接入设置变更需重启。':'已恢复上一版配置');});
}
function confirmAction(title,text,action){$('#confirm-title').textContent=title;$('#confirm-text').textContent=text;const dialog=$('#confirm-dialog');openDialog(dialog);$('#confirm-cancel').onclick=()=>closeDialog(dialog);$('#confirm-ok').onclick=e=>guarded(async()=>{await action();closeDialog(dialog);},e.currentTarget);}
async function logs() {
 const labels={budget_warning:'预算预警（不拦截）',qzone_draft_created:'创建空间草稿',qzone_draft_edited:'编辑空间草稿',qzone_draft_reviewed:'审批空间草稿',qzone_publish_result:'空间发布结果',diary_generated:'生成本地日记',diary_edited:'编辑日记角色表达',diary_deleted:'删除日记及活动',diary_preference_changed:'修改个人日记偏好',diary_forgotten:'删除个人会话日记',diary_capture_failed:'记录日记活动未完成',diary_task_failed:'日记任务未完成',proactive_user_preference:'用户修改主动联系偏好',proactive_contact_edited:'修改主动联系管理限制',proactive_job_created:'创建主动私聊草稿',proactive_job_reviewed:'审批主动私聊任务',proactive_job_finished:'主动私聊任务结束',proactive_scheduler_failed:'主动调度检查出现错误',memory_created:'创建记忆候选',memory_edited:'审核与编辑记忆',memory_deleted:'删除记忆',memory_forgotten:'清理个人会话记忆',memory_preference_changed:'修改个人记忆开关',emotion_edited:'编辑模拟情绪',relationship_created:'新增会话关系',relationship_edited:'编辑会话关系',relationship_deleted:'删除会话关系',social_state_failed:'社交状态更新未完成',emoji_refreshed:'更新表情索引',emoji_uploaded:'上传本地表情',emoji_labels_saved:'保存表情标签',meme_service_action:'管理表情包服务',meme_service_failed:'表情包服务出现问题',meme_generated:'生成表情图片',image_send_blocked:'图片发送被权限拦截',image_send_failed:'图片发送未完成',llm_started:'开始调用模型',llm_completed:'模型调用完成',llm_failed:'模型调用失败',llm_retry:'临时错误重试',llm_fallback:'尝试备用模型',llm_not_configured:'模型尚未配置',qq_sent:'平台确认消息发送',qq_send_failed:'平台消息发送失败',agent_silent:'模型决定保持沉默',agent_model_error:'聊天调用失败',context_cleared:'清理短期上下文',message_queue_full:'消息队列已达上限',agent_failed:'聊天处理内部错误',event_failed:'事件处理内部错误',admin_created:'创建管理员账户',config_saved:'保存配置',config_restored:'恢复配置',secrets_updated:'更新密钥配置',wizard_completed:'完成首次配置',wallpaper_uploaded:'上传壁纸',tool_started:'开始调用工具',tool_completed:'工具调用完成',tool_failed:'工具调用失败'};
 const data=await api('/api/audit');
 mount(`${heading('ACTIVITY','每一次改变，都有迹可循。','这里只记录管理操作，不记录密码、API Key 或聊天正文。')}<section class="panel"><h2>最近的操作</h2>${data.items.length?data.items.map(item=>`<div class="log-item"><span class="log-time">${esc(item.created_at)} UTC</span><span>${esc(labels[item.action]||item.action)}</span></div>`).join(''):'<p class="empty-state">还没有操作记录。</p>'}</section>`);
}
function version() {
 mount(`${heading('ABOUT SOULBOT','保持好奇，慢慢生长。','人格、记忆与社交能力，正在一步步构建。')}
 <section class="panel"><div class="section-title" style="margin-top:0"><h2>SoulBot ${esc(B.version)}</h2>${badge('周期日记、搜索与稳定部署','good')}</div>
 <p class="muted">原生 WebUI · FastAPI · NoneBot2 · SQLite</p><article class="release"><div class="mini-label">2026.10.28 / SEARCH & RECOVERY</div><h3 style="margin-top:10px">更多可用能力，更清楚的运行边界。</h3>
 <ul><li>搜索 Provider、公开主题探索、模型记忆候选、周摘要、日记润色和独立推送审批。</li><li>人格模板/导入导出、全局暂停、私有 ZIP 备份和下次启动恢复。</li><li>QQ 空间公开主题 AI 起草、隐私辅助检查、可选审批与无需审批的自主发布。</li><li>72 小时间隔、滚动 7 天最多 2 次；中断或无回执不重发。</li><li>只读核实 NapCat 4.18.38，未知版本或离线保留草稿；真实后台自主发布已获得平台回执。</li><li>本地日记、确认活动计数、独立角色表达、编辑和私人导出。</li><li>用户私聊授权与撤销、免打扰、固定草稿审批和有额度的调度队列。</li><li>显式记忆候选、审核授权、作用域检索、编辑、归档和个人删除。</li><li>可编辑的全局五维模拟情绪、六态状态机、时间恢复与变化趋势。</li><li>按机器人及群/私聊隔离的好感度、称呼、冻结和删除。</li><li>成功互动后按持久冷却/每日额度更新，不改变权限或费用边界。</li><li>独立图片生成接口、单张预览、持久额度和发送前复核。</li><li>本地 ComfyUI 已验证实际 PNG 生成；云端生图仍需对应凭据，配置存在不表示连接成功。</li><li>本地 Whisper 与独立云端转写接口，上传实测和 QQ 语音预过滤。</li><li>持久音频秒数/次数限额，与模型和 TTS 统一统计费用并预警。</li><li>语音工坊、实际 Edge 音色与独立兼容语音接口。</li><li>语音试听、下载、主人确认发送，以及持久字符限额与费用预警。</li><li>Chromium 生成中文文字、引用和清单卡片，支持三种配色。</li><li>卡片工坊提供准备环境、实际生成、私有预览和下载。</li><li>Emoji、meme-generator 和三个可选表情模板仓库。</li><li>.env 管理登录账号、密码哈希和反向 / 反代 WS 地址。</li><li>QQ 文本处理、模型工具、会话隔离、Token 与费用预警。</li></ul></article>
 <div class="note warning">TTS、ASR、云端生图及本地 ComfyUI 已接入；各 Provider 以当前实例的真实测试结果为准。M3 已开放好感度、全局情绪和候选长期记忆；主动私聊需用户授权和审批；本地日记、周摘要及公开主题探索已开放，M5 已开放可选审批与自主空间发布，默认关闭；真实后台起草及仅自己可见发布已获得平台回执。真实 QQ 与外部模型联调仍需配置可用账户。</div>
 <div class="actions"><a class="button outline" href="https://github.com/ikun441/Soulbot" target="_blank" rel="noopener noreferrer">GitHub 仓库 ↗</a><a class="button quiet" href="#image">打开图片生成</a></div></section>`);
}
function render(target) {
 page=target;
 if(!B.has_admin){step=target==='setup'?step:0;page='setup';wizard();return;}
 if(!B.authenticated){renderLogin();return;}
 if(target==='dashboard')dashboard();else if(target==='setup')wizard();else if(['qq','model','personality'].includes(target))editorPage(target);else if(target==='emotion')guarded(()=>emotionPage(mediaContext()));else if(target==='relationships')guarded(()=>relationshipsPage(mediaContext()));else if(target==='memory')guarded(()=>memoryPage(mediaContext()));else if(target==='proactive')guarded(()=>proactivePage(mediaContext()));else if(target==='diary')guarded(()=>diaryPage(mediaContext()));else if(target==='qzone')guarded(()=>qzonePage(mediaContext()));else if(target==='search')guarded(()=>searchPage(mediaContext()));else if(target==='system')guarded(()=>systemPage(mediaContext()));else if(target==='behavior')behaviorPage();else if(target==='emoji')guarded(()=>emojiPage(mediaContext()));else if(target==='meme')guarded(()=>memePage(mediaContext()));else if(target==='cards')guarded(()=>renderPage(mediaContext()));else if(target==='tts')guarded(()=>speechPage(mediaContext()));else if(target==='asr')guarded(()=>asrPage(mediaContext()));else if(target==='image')guarded(()=>imagePage(mediaContext()));else if(target==='tools')toolsPage(mediaContext());else if(target==='usage')guarded(usagePage);else if(target==='appearance')appearance();else if(target==='account')guarded(accountPage);else if(target==='logs')guarded(logs);else if(target==='version')version();else{page='dashboard';dashboard();}
}
function mediaContext(){return {mount,heading,field,toggle,select,secret,esc,api,send,guarded,notice,confirmAction,saveEditor,config:()=>C,motion:()=>document.body.dataset.motion==='true'};}
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(!a)return;e.preventDefault();if(dirty){notice('请先保存当前表单，再切换页面。',true);return;}const target=a.hash.slice(1);const backward=nav.findIndex(item=>item[0]===target)<nav.findIndex(item=>item[0]===page);changePage(()=>render(target),backward?'backward':'forward');});
$('#logout').onclick=e=>guarded(async()=>{await send('/api/auth/logout','POST');B.authenticated=false;dirty=false;C=null;webHistory=[];chatController?.abort();$('#wallpaper').replaceChildren();renderLogin();},e.currentTarget);
$('#confirm-dialog').addEventListener('cancel',event=>{event.preventDefault();closeDialog(event.currentTarget);});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',()=>applyAppearance());
guarded(async()=>{await refresh();if(!B.has_admin){C={appearance:{motion:true}};step=0;applyAppearance();render('setup');}else render(B.authenticated?(B.initialized?'dashboard':'setup'):'login');});


function chatPanel(){return `<section class="panel chat-panel"><div class="section-title" style="margin-top:0"><h2>和它聊一会儿</h2><button class="button quiet small" id="clear-chat" type="button">清空试聊</button></div><p class="muted">使用保存的人格与模型。试聊不会发送到 QQ，也不会混入 QQ 会话。</p><div id="chat-messages" class="chat-messages" aria-label="试聊记录"><p class="empty-state">说一句你好，看看它会如何回应。</p></div><form id="chat-form"><label for="chat-text" class="mini-label">输入消息</label><div class="chat-input"><textarea id="chat-text" required maxlength="4000" rows="2" placeholder="你好，可以介绍一下自己吗？"></textarea><button class="button primary" id="chat-send" type="submit">保存并发送 ${icon('arrow')}</button><button class="button quiet" id="chat-stop" type="button" hidden>停止</button></div></form><p class="row-detail" id="chat-status">当前试聊仅使用本页短期记录。每次调用都会记录 Token 与费用，达到阈值时仅预警。</p></section>`;}
function bindModel(){
 const list=$('#model-list');if(list)list.onclick=e=>guarded(async()=>{if(!await saveEditor())return;const result=await send('/api/llm/models','POST');$('#model-ids').innerHTML=result.models.map(name=>`<option value="${esc(name)}"></option>`).join('');$('#model-result').hidden=false;$('#model-result').textContent=`已读取 ${result.models.length} 个服务商实际返回的模型。${result.selected_model_available?'当前模型在列表中。':'请在模型名称字段选择或填写有权限的模型。'}`;},e.currentTarget);
 const test=$('#model-test');if(test)test.onclick=e=>guarded(async()=>{if(!await saveEditor())return;try{const result=await send('/api/llm/test','POST');$('#model-result').hidden=false;$('#model-result').textContent=`${result.message}。回复：${result.text}。费用：${result.cost_cny===null?'未知':result.cost_cny.toFixed(6)+' 元'}${result.estimated?'（用量为保守估算）':''}`;health=await api('/api/health');}catch(error){const node=$('#model-result');node.hidden=false;node.textContent=providerErrorText(error.kind,error.message);throw error;}},e.currentTarget);
 const toolsTest=$('#model-tools-test');if(toolsTest)toolsTest.onclick=()=>confirmAction('验证并启用工具调用？','将使用当前主模型进行一次只读工具调用测试，可能计费。成功后启用工具能力。',async()=>{if(!await saveEditor())return;const result=await send('/api/llm/tools-test','POST');C.llm.supports_tools=true;const checkbox=document.querySelector('[data-field="llm.supports_tools"]');if(checkbox)checkbox.checked=true;$('#model-result').hidden=false;$('#model-result').textContent=result.message;});
 const form=$('#chat-form');if(!form)return;
 $('#clear-chat').onclick=()=>{chatController?.abort();webHistory=[];$('#chat-messages').innerHTML='<p class="empty-state">试聊记录已清空。</p>';};
 $('#chat-stop').onclick=()=>chatController?.abort();
 form.onsubmit=e=>{e.preventDefault();guarded(async()=>{
  const text=$('#chat-text').value.trim();if(!text)return;
  if(!await saveEditor())return;
  const messages=$('#chat-messages');messages.querySelector('.empty-state')?.remove();
  const addBubble=(content,kind)=>{const bubble=document.createElement('div');bubble.className='chat-bubble '+kind;bubble.textContent=content;messages.append(bubble);messages.scrollTop=messages.scrollHeight;return bubble;};
  addBubble(text,'human');const output=addBubble('正在思考…','assistant');output.setAttribute('aria-live','polite');
  $('#chat-text').value='';$('#chat-stop').hidden=false;const controller=new AbortController();chatController=controller;
  try{
   const response=await fetch('/api/chat',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Soulbot-Request':'1'},body:JSON.stringify({text,history:webHistory.slice(-16),stream:true}),signal:controller.signal});
   if(!response.ok){const error=await response.json();throw new Error(error.detail||'试聊请求失败');}
   const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',answer='',done=false;
   while(true){const chunk=await reader.read();if(chunk.done)break;buffer+=decoder.decode(chunk.value,{stream:true});let boundary;while((boundary=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,boundary);buffer=buffer.slice(boundary+1);if(!line.trim())continue;const event=JSON.parse(line);
    if(event.type==='delta'){answer+=event.text;output.textContent=answer;messages.scrollTop=messages.scrollHeight;}
    if(event.type==='error')throw new Error(event.message);
    if(event.type==='done'){done=true;answer=event.text;output.textContent=event.silent?'这一次，它选择安静陪伴。':answer;$('#chat-status').textContent=`服务：${event.provider} · ${event.estimated?'用量采用保守估算':'服务商已返回用量'} · 费用 ${event.cost_cny===null?'未知':event.cost_cny.toFixed(6)+' 元'}`;if(answer)webHistory=[...webHistory,{role:'user',content:text},{role:'assistant',content:answer}].slice(-16);}
   }}
   if(!done)throw new Error('响应中断，本次用量已保守保留');
  }catch(error){output.textContent=error.name==='AbortError'?'已停止生成；已发生或无法确认的消耗会保守记账。':`未能完成回复：${error.message}`;if(error.name!=='AbortError')throw error;
  }finally{if(chatController===controller)chatController=null;if($('#chat-stop'))$('#chat-stop').hidden=true;}
 },$('#chat-send'));};
}
function behaviorPage(){
 mount(`${heading('CONVERSATION & BOUNDARIES','每一次回应，都有自己的分寸。','规则先于模型。可以为每个群设置独立模式，随时关闭主动参与。')}<form id="editor" class="panel">${permissionsForm()}${toggle('允许 QQ 私聊回复','behavior.private_chat','关闭后不处理普通私聊；主人管理命令仍遵循接入权限。')}${toggle('允许主动参与群聊','behavior.proactive_group_enabled','还需要逐群授权；默认关闭。价格未知仅预警；免打扰和行为次数限制仍生效。')}<details class="advanced"><summary>频率、聚合与上下文</summary><div class="form-grid" style="margin-top:18px">${field('回复冷却（秒）','behavior.reply_cooldown_seconds','每群独立，私聊按用户独立；重启仍保留。','number','min="0" max="600" step="0.1" required')}${field('群消息聚合窗口（秒）','behavior.group_batch_seconds','最多聚合 20 条候选消息；普通过滤消息不会进入模型。','number','min="0" max="15" step="0.1" required')}${field('连续对话窗口（秒）','behavior.followup_window_seconds','仅同一用户、带指向词且未回复或 @ 其他人时生效。','number','min="0" max="300" required')}${field('主动参与最短间隔（秒）','behavior.proactive_interval_seconds','','number','min="180" max="86400" required')}${field('每群每日主动尝试上限','behavior.max_proactive_per_day','明确 @ 不占主动额度；失败或沉默的主动尝试也计数。','number','min="0" max="10" required')}${field('保留的上下文轮数','behavior.context_turns','按机器人、群和用户隔离，不是长期记忆。','number','min="0" max="20" required')}${field('上下文保留时间（小时）','behavior.context_retention_hours','超过时间自动过期，默认 24 小时。','number','min="1" max="168" required')}${field('单次输入上限','limits.max_input_per_request','按 UTF-8 字节保守估算，实际用量以服务商为准。','number','min="256" max="128000" required')}${field('单次输出 Token 上限','limits.max_output_per_request','','number','min="16" max="8192" required')}${field('单次任务 Token 上限','limits.max_per_agent_task','包含重试与备用服务。','number','min="512" max="300000" required')}${field('群白名单','behavior.group_allowlist','可留空；多个群号用逗号分隔。','text','data-list="true"')}${field('群黑名单','behavior.group_blocklist','黑名单优先。','text','data-list="true"')}${field('需要忽略的机器人 QQ','behavior.ignored_bot_ids','已连接机器人和明确标记的机器人会自动过滤；其它机器人可在此登记。','text','data-list="true"')}${field('额外唤醒词','behavior.wake_words','用逗号分隔；AI 名称默认也是唤醒词。','text','data-list="true"')}</div></details><div class="actions"><button id="save" class="button primary" type="submit">保存全局策略</button><button class="button quiet" id="clear-context" type="button">清理短期上下文</button></div></form><section class="panel" style="margin-top:20px"><h2>逐群设置</h2><p class="muted">新群默认继承全局被动策略。主动参与需要全局开关与本群授权同时开启。</p><div class="form-grid" data-policy-form><div class="field"><label for="policy-id">群号</label><input id="policy-id" pattern="[1-9][0-9]{4,11}" placeholder="填写要管理的群号"></div><div class="field"><label for="policy-mode">群聊模式</label><select id="policy-mode"><option value="mention_only">被动回应</option><option value="silent">静默（仅管理命令）</option><option value="natural">自然社交（8% 候选）</option><option value="active">活跃社交（20% 候选）</option></select></div><label class="switch-row"><span>允许本群接入</span><input type="checkbox" id="policy-enabled" checked></label><label class="switch-row"><span>授权本群主动参与</span><input type="checkbox" id="policy-proactive"></label></div><div class="actions"><button class="button outline" id="save-policy">保存本群策略</button></div><div id="policy-list" class="rows"></div><div class="note">群管理员或主人可在 QQ 使用：<code>/soulbot 状态</code>、<code>/soulbot 静音 15</code>、<code>/soulbot 恢复</code>。静默模式不调用模型。</div></section>`);
 const showPolicies=()=>{$('#policy-list').innerHTML=Object.entries(C.behavior.group_policies).map(([id,p])=>`<div class="row"><div><div class="row-title">群 ${esc(id)}</div><div class="row-detail">${esc({silent:'静默',mention_only:'被动',natural:'自然社交',active:'活跃社交'}[p.mode])} · ${p.enabled?'已启用':'已关闭'} · 主动授权${p.allow_proactive?'开启':'关闭'}</div></div><div class="policy-actions"><button class="button quiet small" data-edit-policy="${id}">编辑</button><button class="button quiet small" data-remove-policy="${id}">移除</button></div></div>`).join('')||'<p class="empty-state">还没有独立群策略，当前继承全局设置。</p>';};showPolicies();
 $('#save-policy').onclick=e=>guarded(async()=>{const input=$('#policy-id');if(!input.value||!input.reportValidity())throw new Error('请填写有效群号');const id=input.value.trim();C.behavior.group_policies[id]={enabled:$('#policy-enabled').checked,mode:$('#policy-mode').value,allow_proactive:$('#policy-proactive').checked};if(await saveEditor())showPolicies();},e.currentTarget);
 $('#policy-list').onclick=e=>{const edit=e.target.closest('[data-edit-policy]'),remove=e.target.closest('[data-remove-policy]');if(edit){const id=edit.dataset.editPolicy,p=C.behavior.group_policies[id];$('#policy-id').value=id;$('#policy-enabled').checked=p.enabled;$('#policy-mode').value=p.mode;$('#policy-proactive').checked=p.allow_proactive;}if(remove){const id=remove.dataset.removePolicy;confirmAction('移除独立群策略？','移除后本群将继承全局策略，原来的关闭状态也会失效。',async()=>{delete C.behavior.group_policies[id];if(await saveEditor())showPolicies();});}};
 $('#clear-context').onclick=()=>confirmAction('清理全部短期上下文？','会删除 QQ 私聊和群聊保留的短期文本。用量、冷却和每日次数不会重置。',async()=>{await api('/api/context',{method:'DELETE'});notice('短期上下文已清理，额度与冷却保持不变');});
}
async function usagePage(){
 const data=await api('/api/usage');
 const metrics=(title,value,note)=>`<article class="panel metric"><div class="metric-head">${title}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></article>`;
 const status={pending:'已预留',completed:'实际用量',estimated:'保守估算',uncertain:'消耗未确认',rejected:'请求被拒绝'};
 const warningNames={daily_tokens:'当日 Token',daily_cost:'当日费用',request_input:'单次输入 Token',task_tokens:'单任务 Token',unknown_cost:'未知费用'};
 const warningText=item=>item.code==='unknown_cost'?`有 ${item.value} 次未知费用调用，请核对服务商账单；调用继续。`:`${warningNames[item.code]||item.code} ${item.value} / ${item.threshold}：${item.severity==='exceeded'?'已达到预警阈值':'接近预警阈值'}，调用继续。`;
 mount(`${heading('USAGE & COST','看见每一份消耗。','每次真实调用、重试和备用服务都有记录。价格未知会明确标出。')}<div class="metric-grid">${metrics('今日 Token',data.reserved_or_used_tokens,`预警阈值 ${data.token_limit}（不拦截）`)}${metrics('已知 / 估算费用',data.known_or_estimated_cost_cny.toFixed(6)+' 元',data.unknown_cost_calls?'另有未知费用，不能视为总费用':'仅按你配置的服务商价格计算')}${metrics('未知费用调用',data.unknown_cost_calls,'价格未配置时不显示为免费')}${metrics('每日费用预警阈值',data.cost_limit_cny+' 元',`统计日 ${data.day}`)}</div><section class="panel" style="margin-top:20px"><div class="section-title" style="margin-top:0"><h2>最近的模型调用</h2><button class="button quiet small" id="refresh-usage">刷新</button></div><p class="muted">预算仅预警，不拦截调用。重启保留统计；超时、中断和缺少用量时会保守记账。</p><div class="usage-table">${data.recent.length?`<table><thead><tr><th>服务 / 模型</th><th>状态</th><th>输入</th><th>输出</th><th>推理</th><th>费用</th></tr></thead><tbody>${data.recent.map(item=>`<tr><td>${esc(item.provider)}<small>${esc(item.model)}</small></td><td>${esc(status[item.status]||item.status)}</td><td>${item.input_tokens}</td><td>${item.output_tokens}</td><td>${item.reasoning_tokens??'未提供'}</td><td>${item.cost_cny===null?'未知':item.cost_cny.toFixed(6)+' 元'}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">还没有模型调用。可以先到 AI 模型页面运行连接测试。</p>'}</div></section>`);
 $('#main').insertAdjacentHTML('beforeend',`<section class="panel media-section" id="budget-warnings"><h2>预算预警（不拦截）</h2><p class="note">达到阈值的 ${Math.round(data.warning_ratio*100)}% 开始预警；费用和 Token 超阈值、价格未知均继续调用。</p>${data.warnings.map(item=>`<p class="note warning">${esc(warningText(item))}</p>`).join('')||'<p class="row-detail">当前没有当日费用或 Token 预警。</p>'}<details><summary>本日预警记录</summary>${data.recent_warnings.map(item=>`<p class="row-detail">${esc(warningText(item))}</p>`).join('')||'<p>尚无预警。</p>'}</details></section>`);
 $('#main').insertAdjacentHTML('beforeend',`<section class="panel media-section"><h2>语音生成记录</h2><p class="note">今日 ${data.tts.calls} 次调用 · ${data.tts.chars} 字符。字符数独立于 Token；费用已纳入上方总额。Edge 无 API 报价费用，云端价格未知时明确标记未知。</p><div class="usage-table">${data.tts.recent.length?`<table><thead><tr><th>服务 / 模型</th><th>状态</th><th>字符</th><th>费用</th></tr></thead><tbody>${data.tts.recent.map(item=>`<tr><td>${esc(item.provider)}<small>${esc(item.model)}</small></td><td>${esc(status[item.status]||item.status)}</td><td>${item.chars}</td><td>${item.cost_cny===null?'未知':item.cost_cny.toFixed(6)+' 元'}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">还没有语音调用记录。</p>'}</div></section>`);
 $('#main').insertAdjacentHTML('beforeend',`<section class="panel media-section"><h2>语音识别记录</h2><p class="note">今日 ${data.asr.calls} 次识别 · ${data.asr.seconds.toFixed(2)} 音频秒数。费用纳入共享预算；录音与识别正文不写入此记录。</p><div class="usage-table">${data.asr.recent.length?`<table><thead><tr><th>服务 / 模型</th><th>状态</th><th>音频秒数</th><th>费用</th></tr></thead><tbody>${data.asr.recent.map(item=>`<tr><td>${esc(item.provider)}<small>${esc(item.model)}</small></td><td>${esc(status[item.status]||item.status)}</td><td>${item.seconds.toFixed(2)}</td><td>${item.cost_cny===null?'未知':item.cost_cny.toFixed(6)+' 元'}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">还没有识别记录。</p>'}</div></section>`);
 $('#main').insertAdjacentHTML('beforeend',`<section class="panel media-section"><h2>图片生成记录</h2><p class="note">今日 ${data.image_generation.calls} 次调用 · ${data.image_generation.images} 张尝试额度。未知报价不视为零费用；取消和超时可能已计费。</p><div class="usage-table">${data.image_generation.recent.length?`<table><thead><tr><th>服务 / 模型</th><th>状态</th><th>图片数</th><th>费用</th></tr></thead><tbody>${data.image_generation.recent.map(item=>`<tr><td>${esc(item.provider)}<small>${esc(item.model)}</small></td><td>${esc(status[item.status]||item.status)}</td><td>${item.images}</td><td>${item.cost_cny===null?'未知':item.cost_cny.toFixed(6)+' 元'}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">还没有图片生成调用。</p>'}</div></section>`);
 $('#refresh-usage').onclick=e=>guarded(usagePage,e.currentTarget);
}

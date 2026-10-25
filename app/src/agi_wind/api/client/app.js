import { AgiWindClient, ApiError } from './sdk/api.js?v=20261026c';
import { appendImages, messageImages } from './media.js?v=20261026c';
import { cropAvatar } from './avatar-crop.js?v=20261026c';
import { splitReply } from './reply-segments.js?v=20261026c';
import { createAudioMessage, stopAllAudio } from './audio.js?v=20261026c';
import { createAgentPage } from './agent-settings.js';

const client = new AgiWindClient();
const agentPage = createAgentPage(client);
const DEFAULT_KEY = '123456';
const SERVER_STORAGE = 'agiwind-server';
const MODEL_TABS = [
  { key: 'dialogue', label: '计划 / 对话模型', role: 'dialogue', capability: 'text', ready: true, hint: '主对话模型，也用于计划与整段回复。' },
  { key: 'asr', label: '语音转文字', role: 'asr', capability: 'asr', ready: false, hint: '语音识别适配器尚未接入；现在保存配置不会生效。' },
  { key: 'tts', label: '文字转语音', role: 'tts', capability: 'tts', ready: false, hint: '本地 Breeze-TTS-2 语音服务；在这里配置音色、概率与试听。' },
  { key: 'embedding', label: '嵌入', role: 'embedding', capability: 'embedding', ready: false, hint: '向量嵌入尚未接入；当前记忆检索使用中文关键词 + 精确子串。' },
  { key: 'rerank', label: '重排序', role: 'rerank', capability: 'rerank', ready: false, hint: '重排序适配器尚未接入；现在保存配置不会生效。' },
  { key: 'vision', label: '识图', role: 'vision', capability: 'image', ready: true, hint: '图片会先保存，再由视觉模型生成观察资料进入上下文。' },
];

const state = {
  companionId: null,
  companionName: '',
  conversation: null,
  persona: null,
  streaming: null,
  pendingMemory: null,
  settings: null,
  personaName: '',
  userName: '',
  avatarAssetId: null,
  avatarPending: undefined,
  userAvatarAssetId: null,
  userAvatarPending: undefined,
  modelTab: 'dialogue',
  selectedProviderId: null,
  serverMode: false,
  serverAddress: '',
  serverKey: '',
  readyStatus: '',
  readyDeps: {},
  archiveRows: [],
  archiveCount: 0,
  lastArchive: '',
  archiveViewId: null,
  personaBytes: 0,
  memoryRevision: 0,
  memoryItems: [],
  selectedMemoryId: null,
  memoryToken: 0,
  memoryQuery: '',
  memoryKind: '',
  memoryStatus: 'active',
  memorySort: 'created',
  memoryJobs: [],
  memoryStats: null,
  memorySettings: null,
  currentPage: 'chat',
  themeChoice: 'system',
  logEntries: [],
  logOpen: false,
  logUnread: 0,
  termLines: [],
  termFilter: 'all',
  termSearch: '',
  termDetail: true,
  termFollow: true,
  termOffsets: { stdout: null, stderr: null },
  termTimer: null,
  termLastError: '',
  termSeeded: false,
  lastServiceOnline: undefined,
  pendingImages: [],
  uploadingImages: false,
  busy: false,
  voiceAlways: false,
  voiceProbability: 30,
  voiceSettings: null,
  voiceVoices: [],
  voiceWatchers: new Map(),
  ttsPreviewUrl: null,
  processingLines: new Map(),
  processRenderPending: false,
  logWidth: 420,
  historyLogged: new Set(),
  liveWatch: null,
  seenProactive: new Set(),
};

const LOG_KEY = 'agiwind-log-v1';
const LOG_LIMIT = 200;
const VOICE_ALWAYS_KEY = 'agiwind-voice-always';
try { state.voiceAlways = localStorage.getItem(VOICE_ALWAYS_KEY) === '1'; } catch {}

const PAGES = {
  chat: { label: '对话', load: () => loadChatPage() },
  archive: { label: '对话归档', load: () => loadArchive() },
  soul: { label: '灵魂档案', load: () => loadPersona().catch(() => {}) },
  memory: { label: '记忆', load: () => loadMemoryPage() },
  stickers: { label: '表情包库', load: () => loadStickers() },
  model: { label: '模型与密钥', load: () => loadModelPage() },
  agent: { label: 'Agent 能力', load: () => agentPage.load() },
  connection: { label: '连接', load: () => loadConnectionPage() },
  appearance: { label: '外观与账户', load: () => updateAppearancePage() },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
  const r = (Math.random() * 16) | 0;
  return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
}));
const formatTime = (value) => {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16).replace('T', ' ');
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString('zh-CN', { hour12: false })
    : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + ' ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
};

let toastTimer = null;
function toast(message, kind = '') {
  const node = $('#toast');
  node.textContent = message;
  node.className = 'toast' + (kind ? ' ' + kind : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
}

/* ---------- 系统日志终端 ---------- */
function loadLogEntries() {
  try {
    const raw = JSON.parse(localStorage.getItem(LOG_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((entry) => entry && typeof entry.ts === 'string' && typeof entry.title === 'string').slice(-LOG_LIMIT);
  } catch {
    return [];
  }
}

function persistLogEntries() {
  try { localStorage.setItem(LOG_KEY, JSON.stringify(state.logEntries)); } catch {}
}

function formatLogTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

function stripHiddenReplyPrefix(value) {
  return String(value || '').replace(
    /^\s*[\[【]\s*(?:情绪\s*[:：][^\]】\n]{1,12}|开心|平静|难过|担心|撒娇|生气|惊讶|温柔|高兴|快乐|喜悦|悲伤|伤心|失落|害怕|焦虑|愤怒|不满|吃惊|意外|柔和|亲切)\s*[\]】]\s*/,
    '',
  );
}

function appendTermLine(line, { defer = false } = {}) {
  const entry = {
    cat: ['sys','proc'].includes(line.cat) ? line.cat : 'act',
    level: ['info', 'warn', 'error'].includes(line.level) ? line.level : 'info',
    tag: line.tag || (line.cat === 'sys' ? 'OUT' : 'ACT'),
    group: line.group || (line.cat === 'sys' ? 'sys' : ''),
    stream: line.stream || '',
    text: String(line.text || ''),
    fields: line.fields && typeof line.fields === 'object' ? line.fields : null,
    ts: line.ts || new Date().toISOString(),
  };
  if (!entry.text) return;
  state.termLines.push(entry);
  if (state.termLines.length > 1500) state.termLines = state.termLines.slice(-1500);
  if (state.logOpen && !defer) renderTerm();
  return entry;
}

// Processing text is only in this page's in-memory drawer, never localStorage/stdout.
function logProcessing(generationId,type,payload) {
  const stage=payload.stage==='reflect'?'回答后':'回答前';
  if(type==='reasoning.delta'||type==='text.delta'){
    const key=generationId+':'+type+':'+payload.stage;
    let line=state.processingLines.get(key);
    if(!line||!state.termLines.includes(line)){
      line=appendTermLine({cat:'proc',tag:type==='text.delta'?'REPLY':'THINK',text:(type==='text.delta'?'回复正文':stage)+' · '},{defer:true});
      state.processingLines.set(key,line);
      if(state.processingLines.size>100)state.processingLines.delete(state.processingLines.keys().next().value);
    }
    line.text=line.text+(payload.text||'');
  }else{
    const toolName=payload.tool||payload.name||payload.data?.tool||'';
    if(!toolName)return;
    const rawStatus=payload.status||payload.data?.status;
    const status=type==='tool.started'?'进行中':({completed:'完成',failed:'失败',skipped:'已跳过'}[rawStatus]||rawStatus||'完成');
    const summary=payload.summary||payload.message||payload.data?.summary||'';
    const text=stage+' · '+toolName+' · '+status+(summary?' · '+summary:'');
    const last=state.termLines[state.termLines.length-1];
    if(last&&last.tag==='TOOL'&&last.text===text&&Date.now()-new Date(last.ts).getTime()<3000)return;
    appendTermLine({cat:'proc',tag:'TOOL',level:rawStatus==='failed'?'error':'info',text},{defer:true});
  }
  if(!state.logOpen){state.logUnread++;updateLogBadge();return;}
  if(state.processRenderPending)return;
  state.processRenderPending=true;
  requestAnimationFrame(()=>{state.processRenderPending=false;if(state.logOpen)renderTerm();});
}

function logHistoryProcessing(event) {
  if(event.kind!=='assistant_output')return;
  const key=event.generation_id||event.id;
  if(!key||state.historyLogged.has(key))return;
  state.historyLogged.add(key);
  let replied=false;
  for(const step of event.trace||event.content?.trace||[]){
    if(!replied&&(step.stage==='reflect'||step.tool==='reply')){
      logProcessing(key,'text.delta',{stage:'reply',text:stripHiddenReplyPrefix(event.content?.text||'')});replied=true;
    }
    if(step.type==='reasoning')logProcessing(key,'reasoning.delta',step);
    else if(step.type==='tool')logProcessing(key,'tool.completed',step);
  }
  if(!replied&&event.content?.text)logProcessing(key,'text.delta',{stage:'reply',text:stripHiddenReplyPrefix(event.content.text)});
}

function setLogWidth(value,{persist=false}={}) {
  const sidebar=window.innerWidth>1180?234:(window.innerWidth>760?210:0);
  const max=Math.max(280,window.innerWidth-sidebar-(sidebar?320:64));
  state.logWidth=Math.round(Math.max(280,Math.min(max,Number.isFinite(value)?value:420)));
  document.documentElement.style.setProperty('--log-width',state.logWidth+'px');
  const handle=$('#log-resize');
  handle.setAttribute('aria-valuemin','280');handle.setAttribute('aria-valuemax',String(max));handle.setAttribute('aria-valuenow',String(state.logWidth));
  if(persist)try{localStorage.setItem('agiwind-log-width',String(state.logWidth));}catch{}
}

function initLogResize() {
  let saved=420;try{saved=Number(localStorage.getItem('agiwind-log-width'))||420;}catch{}
  setLogWidth(saved);
  const handle=$('#log-resize');let drag=null;
  handle.addEventListener('pointerdown',event=>{
    if(event.button!==0||window.innerWidth<=760)return;
    event.preventDefault();drag={x:event.clientX,width:state.logWidth};handle.setPointerCapture(event.pointerId);document.body.classList.add('resizing-log');
  });
  handle.addEventListener('pointermove',event=>{if(drag)setLogWidth(drag.width+drag.x-event.clientX);});
  const finish=()=>{if(!drag)return;drag=null;document.body.classList.remove('resizing-log');setLogWidth(state.logWidth,{persist:true});};
  handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);handle.addEventListener('lostpointercapture',finish);
  handle.addEventListener('dblclick',()=>setLogWidth(420,{persist:true}));
  handle.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home'].includes(event.key))return;event.preventDefault();setLogWidth(event.key==='Home'?420:state.logWidth+(event.key==='ArrowLeft'?20:-20),{persist:true});});
  window.addEventListener('resize',()=>setLogWidth(state.logWidth));
}

function logEvent(category, title, detail = '', fields = null) {
  const last = state.logEntries[state.logEntries.length - 1];
  if (last && last.category === category && last.title === title && last.detail === String(detail || '').slice(0, 500)
    && Date.now() - new Date(last.ts).getTime() < 4000) return;
  const entry = {
    id: Date.now() + Math.random(),
    ts: new Date().toISOString(),
    category: ['chat', 'error', 'system'].includes(category) ? category : 'system',
    title,
    detail: String(detail || '').slice(0, 500),
    fields: fields && typeof fields === 'object' ? fields : null,
  };
  state.logEntries.push(entry);
  if (state.logEntries.length > LOG_LIMIT) state.logEntries = state.logEntries.slice(-LOG_LIMIT);
  persistLogEntries();
  appendTermLine({
    cat: 'act',
    level: entry.category === 'error' ? 'error' : 'info',
    tag: 'ACT',
    text: entry.title + (entry.detail ? ' · ' + entry.detail : ''),
    fields: entry.fields,
    ts: entry.ts,
  });
  if (!state.logOpen) {
    state.logUnread += 1;
    updateLogBadge();
  }
}

function updateLogBadge() {
  const badge = $('#log-badge');
  if (!badge) return;
  badge.hidden = state.logUnread <= 0;
  badge.textContent = state.logUnread > 99 ? '99+' : String(state.logUnread);
}

function seedTermFromActivity() {
  if (state.termSeeded) return;
  state.termSeeded = true;
  const seen = new Set(state.termLines.map((line) => line.ts + '|' + line.text));
  for (const entry of state.logEntries.slice(-80)) {
    const text = entry.title + (entry.detail ? ' · ' + entry.detail : '');
    const key = entry.ts + '|' + text;
    if (seen.has(key)) continue;
    appendTermLine({
      cat: 'act',
      level: entry.category === 'error' ? 'error' : 'info',
      tag: 'ACT',
      text,
      fields: entry.fields,
      ts: entry.ts,
    }, { defer: true });
  }
}

function termLevel(text) {
  if (/\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\b/i.test(text) || /失败|错误|异常|崩溃/.test(text)) return 'error';
  if (/\bWARN(ING)?\b/i.test(text) || /警告|超时/.test(text)) return 'warn';
  return 'info';
}

const SYSTEM_GROUPS = {
  api: '接口',
  gen: '生成',
  voice: '语音',
  memory: '记忆',
  agent: 'Agent',
  proactive: '主动',
  sys: '系统',
};

function systemLineGroup(raw) {
  const module = (String(raw).match(/\[(agi_wind[^\]]*)\]/) || [])[1] || '';
  const name = module.toLowerCase();
  if (/voice|tts/.test(name)) return 'voice';
  if (/memory/.test(name)) return 'memory';
  if (/agent/.test(name)) return 'agent';
  if (/proactive/.test(name)) return 'proactive';
  if (/delivery/.test(name)) return 'gen';
  if (/^http\b/i.test(String(raw).trim()) || /\bhttp\s+(?:GET|POST|PUT|PATCH|DELETE)\b/i.test(String(raw))) return 'api';
  return 'sys';
}

function localizeSystemText(raw) {
  let text = String(raw).replace(/\[agi_wind(?:\.[a-z_]+)*\]\s*/g, '').replace(/^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+/i, '').trim();
  const translations = [
    [/^http (\w+) (\S+) -> (\d+) (\d+)ms$/, (_, method, path, status, ms) => `接口 ${method} ${path} · ${status} · ${ms}ms`],
    [/^generation (\S+) running provider=(\S+) model=(\S+)$/, (_, id, provider, model) => `开始生成 · ${provider}/${model}`],
    [/^generation (\S+) completed$/, () => '生成完成'],
    [/^generation (\S+) failed code=(\S+)$/, (_, id, code) => `生成失败 · ${code}`],
    [/^voice synthesized voice_id=(\S+) seconds=([\d.]+) total_ms=(\d+) rtf=([\d.]+) first_audio_ms=(\S+)$/, (_, voice, seconds, ms, rtf, first) => `语音合成完成 · 音色 ${voice} · ${seconds}s · 耗时 ${ms}ms · 首音 ${first}ms · RTF ${rtf}`],
    [/^voice settings updated enabled=(\w+) voice=(\S+) probability=([\d.]+)$/, (_, enabled, voice, probability) => `语音设置已更新 · ${enabled === 'True' ? '开启' : '关闭'} · 音色 ${voice} · 概率 ${Math.round(Number(probability) * 100)}%`],
    [/^voice health check failed$/, () => '语音健康检查失败'],
    [/^voice director fallback failed generation=(\S+)$/, () => '语音情绪兜底失败'],
    [/^sticker auto-tag failed asset=(\S+)$/, (_, asset) => `表情包自动标签失败 · ${String(asset).slice(0, 8)}`],
    [/^memory extracted companion=(\S+) events=(\d+) candidates=(\d+) stored=(\d+) reinforced=(\d+) quarantined=(\d+) ignored=(\d+) calls=(\S+)$/, (_, companion, events, candidates, stored, reinforced, quarantined, ignored, calls) => `记忆提炼 · 事件 ${events} · 候选 ${candidates} · 写入 ${stored} · 强化 ${reinforced} · 隔离 ${quarantined} · 调用 ${calls}`],
    [/^proactive message sent companion=(\S+) conversation=(\S+) trigger=(\S+)$/, () => '主动消息已发送'],
    [/^proactive settings updated enabled=(\w+) idle=(\d+) gap=(\d+) max=(\d+)$/, () => '主动消息设置已更新'],
    [/^client key session created from (\S+)$/, (_, ip) => `客户端密钥登录 · ${ip}`],
    [/^provider (\S+) saved$/, (_, provider) => `模型厂商已保存 · ${provider}`],
    [/^provider (\S+) deleted$/, (_, provider) => `模型厂商已删除 · ${provider}`],
    [/^model role (\S+) -> (\S+)\/(\S+)$/, (_, role, provider, model) => `模型角色已设置 · ${role} → ${provider}/${model}`],
    [/^conversation started (\S+)$/, () => '对话已创建'],
    [/^conversation (\S+) state -> (\S+)$/, (_, id, state) => `对话状态 · ${state}`],
  ];
  for (const [pattern, replace] of translations) {
    if (pattern.test(text)) return text.replace(pattern, replace);
  }
  return text;
}

function appendSystemLines(lines, reset) {
  if (reset) state.termLines = state.termLines.filter((line) => line.cat !== 'sys');
  let added = 0;
  for (const line of lines || []) {
    let text = String(line.text || '');
    if (!text.trim()) continue;
    let ts = new Date().toISOString();
    const stamped = text.match(/^(\d{2}):(\d{2}):(\d{2})\s+(.*)$/);
    if (stamped) {
      const date = new Date();
      date.setHours(Number(stamped[1]), Number(stamped[2]), Number(stamped[3]), 0);
      ts = date.toISOString();
      text = stamped[4];
    }
    const group = systemLineGroup(text);
    appendTermLine({
      cat: 'sys',
      group,
      stream: line.stream,
      level: termLevel(text),
      tag: SYSTEM_GROUPS[group] || '系统',
      text: localizeSystemText(text),
      ts,
    }, { defer: true });
    added += 1;
  }
  return added;
}

function termLevelLabel(level) {
  return level === 'error' ? 'ERR' : level === 'warn' ? 'WARN' : 'INFO';
}

function filteredTermRows() {
  const query = state.termSearch.trim().toLowerCase();
  return state.termLines.filter((row) => {
    if (state.termFilter === 'error' && row.level !== 'error') return false;
    if (state.termFilter === 'warn' && row.level !== 'warn') return false;
    if (!['all', 'error', 'warn', 'sys', 'proc', 'act'].includes(state.termFilter) && (row.group || row.cat) !== state.termFilter) return false;
    if (['sys', 'proc', 'act'].includes(state.termFilter) && row.cat !== state.termFilter) return false;
    if (!query) return true;
    const searchable = [row.text, row.tag, row.level, row.stream, row.fields ? JSON.stringify(row.fields) : ''].join(' ').toLowerCase();
    return searchable.includes(query);
  });
}

function renderTermFields(fields) {
  const entries = Object.entries(fields || {}).filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!entries.length) return null;
  return el('div', { class: 'term-fields' }, entries.slice(0, 12).map(([key, value]) => el('div', { class: 'term-field' }, [
    el('span', { class: 'term-field-key', text: key }),
    el('span', { class: 'term-field-value', text: typeof value === 'object' ? JSON.stringify(value) : String(value) }),
  ])));
}

function updateTermControls({ scroll = false } = {}) {
  const scrollBox = $('#log-scroll');
  if (scroll && state.termFollow) {
    requestAnimationFrame(() => { scrollBox.scrollTop = scrollBox.scrollHeight; });
  }
  $('#term-jump').hidden = state.termFollow;
  const visible = filteredTermRows();
  const errors = state.termLines.filter((line) => line.level === 'error').length;
  const warnings = state.termLines.filter((line) => line.level === 'warn').length;
  const systemCount = state.termLines.filter((line) => line.cat === 'sys').length;
  const activityCount = state.termLines.length - systemCount;
  $('#log-foot-meta').textContent = '显示 ' + visible.length + '/' + state.termLines.length + ' 行 · 系统 ' + systemCount + ' · 活动 ' + activityCount + ' · 错误 ' + errors + ' · 警告 ' + warnings + ' · ' + (state.termFollow ? '自动跟随' : '已暂停');
  $('#log-status').textContent = '显示 ' + visible.length + '/' + state.termLines.length + ' 行 · ' + (state.termFollow ? '自动跟随' : '已暂停');
  $('#log-search-count').textContent = state.termSearch ? visible.length + '/' + state.termLines.length : '';
  $('#term-follow').textContent = state.termFollow ? '跟随' : '暂停';
  $('#term-follow').classList.toggle('active', state.termFollow);
  $('#term-detail').classList.toggle('active', state.termDetail);
}

function renderTerm() {
  const output = $('#term-output');
  if (!output) return;
  const rows = filteredTermRows();
  output.replaceChildren();
  if (!rows.length) {
    output.append(el('div', { class: 'term-placeholder', text: state.termSearch || state.termFilter !== 'all' ? '# 当前筛选没有匹配日志。' : '# 暂无日志。系统输出与客户端活动会实时滚动到这里。' }));
  } else {
    for (const row of rows) {
      const entry = el('div', {
        class: 'term-entry',
        'data-cat': row.cat,
        'data-group': row.group || row.cat,
        'data-level': row.level,
        'data-stream': row.stream || '',
      }, [
        el('div', { class: 'term-line' }, [
          el('span', { class: 'term-time', text: formatLogTime(row.ts) }),
          el('span', { class: 'term-level', text: termLevelLabel(row.level) }),
          el('span', { class: 'term-tag', text: row.tag }),
          el('span', { class: 'term-text', text: row.text }),
        ]),
      ]);
      if (state.termDetail) {
        const fields = renderTermFields(row.fields);
        if (fields) entry.append(fields);
      }
      output.append(entry);
    }
  }
  updateTermControls({ scroll: state.termFollow });
}

async function copyTermLogs() {
  const rows = filteredTermRows();
  if (!rows.length) { toast('当前没有可复制的日志。'); return; }
  const text = rows.map((row) => {
    const fields = row.fields ? ' ' + JSON.stringify(row.fields) : '';
    return '[' + formatLogTime(row.ts) + '] [' + termLevelLabel(row.level) + '] [' + row.tag + '] ' + row.text + fields;
  }).join('\n');
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制 ' + rows.length + ' 行日志。');
  } catch {
    toast('复制失败：浏览器未授予剪贴板权限。');
  }
}

async function pollSystemLogs({ initial = false } = {}) {
  if (!state.logOpen) return;
  const params = new URLSearchParams();
  const firstLoad = state.termOffsets.stdout === null && state.termOffsets.stderr === null;
  params.set('tail', firstLoad ? '240' : '600');
  if (!firstLoad) {
    params.set('stdout_offset', String(state.termOffsets.stdout));
    params.set('stderr_offset', String(state.termOffsets.stderr));
  }
  try {
    const result = await client.request('/api/v1/system-logs?' + params.toString());
    const added = appendSystemLines(result.lines, Boolean(result.reset));
    state.termOffsets = result.offsets || state.termOffsets;
    state.termLastError = '';
    if (added > 0 || result.reset) renderTerm();
  } catch (error) {
    if (state.termLastError === error.message) return;
    state.termLastError = error.message;
    appendTermLine({ cat: 'sys', level: 'error', tag: 'ERR', text: '读取系统日志失败：' + error.message });
    renderTerm();
  }
}

function syncLogScrim() {
  $('#log-scrim').hidden = !state.logOpen || window.innerWidth > 760;
  const composer=$('.composer-wrap').getBoundingClientRect();
  if(state.currentPage==='chat'&&composer.height>0)document.documentElement.style.setProperty('--composer-clearance',Math.max(0,window.innerHeight-composer.top+1)+'px');
}

function openLog() {
  state.logOpen = true;
  state.logUnread = 0;
  updateLogBadge();
  $('#log-drawer').classList.add('open');
  $('#log-drawer').setAttribute('aria-hidden', 'false');
  $('#log-toggle').setAttribute('aria-expanded', 'true');
  $('#app').classList.add('log-open');
  syncLogScrim();
  seedTermFromActivity();
  renderTerm();
  pollSystemLogs({ initial: true });
  clearInterval(state.termTimer);
  state.termTimer = setInterval(() => pollSystemLogs(), 2000);
}

function closeLog() {
  state.logOpen = false;
  clearInterval(state.termTimer);
  state.termTimer = null;
  $('#log-drawer').classList.remove('open');
  $('#log-drawer').setAttribute('aria-hidden', 'true');
  $('#log-toggle').setAttribute('aria-expanded', 'false');
  $('#app').classList.remove('log-open');
  syncLogScrim();
}

function toggleLog() {
  if (state.logOpen) closeLog();
  else openLog();
}

/* ---------- markdown（先转义，再做最小子集渲染） ---------- */
function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function renderMarkdown(source) {
  const escaped = escapeHtml(source);
  const blocks = [];
  let text = escaped.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_, language, code) => {
    blocks.push('<pre><code>' + code.replace(/\n$/, '') + '</code></pre>');
    return '\u0000BLOCK' + (blocks.length - 1) + '\u0000';
  });
  const inline = (value) => value
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
  const paragraphs = text.split(/\n{2,}/).map((block) => {
    if (/^\u0000BLOCK\d+\u0000$/.test(block.trim())) return block.trim();
    const lines = block.split('\n');
    if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
      return '<ul>' + lines.map((line) => '<li>' + inline(line.replace(/^\s*[-*]\s+/, '')) + '</li>').join('') + '</ul>';
    }
    if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
      return '<ol>' + lines.map((line) => '<li>' + inline(line.replace(/^\s*\d+[.)]\s+/, '')) + '</li>').join('') + '</ol>';
    }
    if (lines.every((line) => /^\s*>\s?/.test(line))) {
      return '<blockquote>' + inline(lines.map((line) => line.replace(/^\s*>\s?/, '')).join('<br>')) + '</blockquote>';
    }
    return '<p>' + inline(lines.join('<br>')) + '</p>';
  });
  return paragraphs.join('').replace(/\u0000BLOCK(\d+)\u0000/g, (_, index) => blocks[Number(index)]);
}

/* ---------- 模态框（不使用浏览器 prompt / confirm） ---------- */
let modalResolver = null;
function openModal({ title, hint = '', input = null, okText = '确定', danger = false }) {
  return new Promise((resolve) => {
    modalResolver = resolve;
    $('#modal-title').textContent = title;
    $('#modal-hint').textContent = hint;
    $('#modal-hint').hidden = !hint;
    const field = $('#modal-input');
    field.hidden = input === null;
    field.value = input === null ? '' : input;
    $('#modal-ok').textContent = okText;
    $('#modal-ok').className = 'btn ' + (danger ? 'danger' : 'primary');
    $('#modal').hidden = false;
    setTimeout(() => { if (input === null) $('#modal-ok').focus(); else { field.focus(); field.select(); } }, 0);
  });
}
function finishModal(value) {
  if (!modalResolver) return;
  const resolve = modalResolver;
  modalResolver = null;
  $('#modal').hidden = true;
  resolve(value);
}
function modalValue() {
  const field = $('#modal-input');
  return field.hidden ? true : field.value.trim();
}

/* ---------- 会话骨架 ---------- */
function showGate(message = '') {
  $('#gate').hidden = false;
  $('#app').hidden = true;
  $('#gate-server').value = state.serverAddress || location.origin;
  $('#gate-key').value = state.serverKey || DEFAULT_KEY;
  if (message) $('#gate-hint').textContent = message;
}

function showApp() {
  $('#gate').hidden = true;
  $('#app').hidden = false;
}

function systemTheme() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function updateThemeCards() {
  const effective = document.documentElement.dataset.theme;
  $$('[data-theme-choice]').forEach((card) => {
    const choice = card.dataset.themeChoice;
    const active = choice === state.themeChoice || (state.themeChoice === 'system' && choice === 'system');
    card.classList.toggle('active', active);
    card.setAttribute('aria-pressed', String(active));
    card.title = choice === 'system' ? '跟随系统（当前 ' + (effective === 'dark' ? '深色' : '浅色') + '）' : '';
  });
}

function applyTheme(choice) {
  state.themeChoice = choice === 'system' ? 'system' : (choice === 'dark' ? 'dark' : 'light');
  const theme = state.themeChoice === 'system' ? systemTheme() : state.themeChoice;
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.themeChoice = state.themeChoice;
  try { localStorage.setItem('agiwind-theme', state.themeChoice); } catch {}
  updateThemeCards();
}

function initTheme() {
  let choice = 'system';
  try { choice = localStorage.getItem('agiwind-theme') || 'system'; } catch {}
  applyTheme(choice);
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (state.themeChoice === 'system') applyTheme('system');
    });
  }
}

function updateAppearancePage() {
  updateThemeCards();
}

function pageFromHash() {
  const name = (location.hash || '').replace(/^#\/?/, '').replace(/\/$/, '');
  return PAGES[name] ? name : 'chat';
}

function renderPage(name, { load = true } = {}) {
  if (!PAGES[name]) name = 'chat';
  $('#app').classList.toggle('chat-active',name==='chat');
  document.body.classList.toggle('chat-layout',name==='chat');
  state.currentPage = name;
  $$('.page').forEach((page) => { page.hidden = page.dataset.page !== name; });
  $$('.nav-item[data-page]').forEach((item) => {
    const active = item.dataset.page === name;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $('#breadcrumb-page').textContent = PAGES[name].label;
  document.title = 'AGI.Wind · ' + PAGES[name].label;
  if (name !== 'chat') closeSessionMenu();
  if (load) PAGES[name].load();
}

function navigate(name, { replace = false } = {}) {
  if (!PAGES[name]) name = 'chat';
  const hash = '#/' + name;
  if (location.hash === hash) { renderPage(name); return; }
  if (replace) {
    history.replaceState(null, '', hash);
    renderPage(name);
  } else {
    // The hashchange listener performs the render; rendering here too would run page loads twice.
    location.hash = hash;
  }
}

function isAudioAttachment(attachment) {
  if (!attachment) return false;
  return attachment.kind === 'audio' || String(attachment.mime || '').startsWith('audio/');
}

function isImageAttachment(attachment) {
  if (!attachment) return false;
  return attachment.kind === 'image' || String(attachment.mime || '').startsWith('image/');
}

function probabilityPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 30;
  return Math.max(0, Math.min(100, Math.round(number <= 1 ? number * 100 : number)));
}

function probabilityPayload(percent) {
  const number = Number(percent);
  return Math.max(0, Math.min(1, (Number.isFinite(number) ? number : 0) / 100));
}

function updateVoiceControls() {
  const probability = Math.max(0, Math.min(100, Math.round(Number(state.voiceProbability) || 0)));
  state.voiceProbability = probability;
  const toggle = $('#voice-toggle');
  if (toggle) toggle.setAttribute('aria-checked', String(Boolean(state.voiceAlways)));
  const ttsRange = $('#tts-probability');
  if (ttsRange) ttsRange.value = String(probability);
  const ttsValue = $('#tts-probability-value');
  if (ttsValue) ttsValue.textContent = probability + '%';
  const breakProbability = Math.round(Number(state.voiceSettings?.text_break_probability ?? 0.35) * 100);
  const breakRange = $('#tts-break-probability');
  if (breakRange) breakRange.value = String(breakProbability);
  const breakValue = $('#tts-break-probability-value');
  if (breakValue) breakValue.textContent = breakProbability + '%';
}

function setVoiceAlways(value, { notify = true } = {}) {
  state.voiceAlways = Boolean(value);
  try { localStorage.setItem(VOICE_ALWAYS_KEY, state.voiceAlways ? '1' : '0'); } catch {}
  updateVoiceControls();
  if (state.voiceAlways && notify && state.voiceSettings && state.voiceSettings.available === false) {
    toast('本地语音服务尚未就绪；开关已保存，稍后可再试。');
  }
}

async function loadVoiceSettings() {
  try {
    const settings = await client.voiceSettings();
    state.voiceSettings = settings;
    state.voiceProbability = probabilityPercent(settings.probability);
    updateVoiceControls();
    return settings;
  } catch {
    state.voiceSettings = null;
    updateVoiceControls();
    return null;
  }
}

function closeVoicePopover() {
  const popover = $('#voice-popover');
  const chip = $('#voice-prob');
  if (popover) popover.hidden = true;
  if (chip) chip.setAttribute('aria-expanded', 'false');
}

function toggleVoicePopover() {
  const popover = $('#voice-popover');
  const chip = $('#voice-prob');
  if (!popover || !chip) return;
  const opening = popover.hidden;
  popover.hidden = !opening;
  chip.setAttribute('aria-expanded', String(opening));
  if (opening) setTimeout(() => $('#voice-prob-range')?.focus(), 0);
}

function logVoiceProcessing(type, payload = {}, generationId = '') {
  const labels = {
    queued: '语音排队',
    synthesizing: '语音合成中',
    ready: '语音已生成',
    failed: '语音失败',
    skipped: '语音跳过',
    cancelled: '语音取消',
  };
  const durationMs = Number(payload.attachment?.duration_ms || 0);
  const parts = [labels[type] || '语音状态'];
  if (durationMs > 0) parts.push((durationMs / 1000).toFixed(1) + 's');
  if (payload.emotion) parts.push('情绪 ' + payload.emotion);
  if (payload.error_code) parts.push(payload.error_code);
  appendTermLine({
    cat: 'proc',
    tag: 'VOICE',
    level: type === 'failed' ? 'error' : 'info',
    text: parts.join(' · '),
    fields: {
      event: 'speech.' + type,
      generation_id: generationId || undefined,
      segment_id: payload.segment_id,
      voice_id: payload.voice_id,
      mode: payload.mode,
      state: payload.state,
      duration_ms: durationMs || undefined,
      emotion: payload.emotion,
      error_code: payload.error_code,
    },
  });
}

function startVoiceWatch(generationId, turn, { segmentId = '', autoplay = false } = {}) {
  if (!generationId || !turn || state.voiceWatchers.has(generationId + ':' + segmentId)) return;
  const watcher = { stopped: false, timer: null };
  const key = generationId + ':' + segmentId;
  state.voiceWatchers.set(key, watcher);
  const startedAt = Date.now();
  let lastState = '';
  const finish = () => {
    watcher.stopped = true;
    if (watcher.timer) clearTimeout(watcher.timer);
    state.voiceWatchers.delete(key);
  };
  const schedule = () => {
    if (!watcher.stopped) watcher.timer = setTimeout(tick, 700);
  };
  const tick = async () => {
    if (watcher.stopped) return;
    if (Date.now() - startedAt > 120000) {
      turn.voice({ state: 'failed', segment_id: segmentId, error_code: 'TIMEOUT' });
      logVoiceProcessing('failed', { segment_id: segmentId, error_code: 'TIMEOUT', state: 'failed' }, generationId);
      finish();
      return;
    }
    try {
      const data = await client.generationSpeech(generationId);
      const next = data || {};
      const nextState = next.state || 'none';
      if (nextState === 'ready') {
        turn.voice({
          state: 'ready',
          attachment: next.attachment,
          segment_id: next.segment_id || segmentId,
          voice_id: next.voice_id,
          mode: next.mode,
          emotion: next.emotion,
          autoplay,
        });
        logVoiceProcessing('ready', next, generationId);
        finish();
        return;
      }
      if (nextState === 'failed' || nextState === 'skipped' || nextState === 'cancelled') {
        turn.voice({
          state: nextState,
          segment_id: next.segment_id || segmentId,
          voice_id: next.voice_id,
          mode: next.mode,
          emotion: next.emotion,
          error_code: next.error_code,
        });
        logVoiceProcessing(nextState, next, generationId);
        finish();
        return;
      }
      if (nextState === 'none') {
        finish();
        return;
      }
      turn.voice({
        state: nextState === 'synthesizing' ? 'synthesizing' : 'queued',
        segment_id: next.segment_id || segmentId,
        voice_id: next.voice_id,
        mode: next.mode,
        emotion: next.emotion,
      });
      if (nextState !== lastState) {
        logVoiceProcessing(nextState, next, generationId);
        lastState = nextState;
      }
      schedule();
    } catch {
      schedule();
    }
  };
  tick();
}

function handleHistoryVoice(event, turn) {
  const voice = event.content?.voice || {};
  const attachments = event.attachments || event.content?.attachments || [];
  if (attachments.some(isAudioAttachment)) return;
  if (voice.state === 'queued' || voice.state === 'synthesizing') {
    turn.voice({
      state: voice.state,
      segment_id: voice.segment_id,
      voice_id: voice.voice_id,
      mode: voice.mode,
      emotion: voice.emotion,
    });
    startVoiceWatch(event.generation_id, turn, { segmentId: voice.segment_id || '', autoplay: false });
  }
}

function stopLiveWatch() {
  const watcher = state.liveWatch;
  state.liveWatch = null;
  if (watcher) {
    try { watcher.controller.abort(); } catch {}
  }
}

function startLiveWatch(conversationId) {
  if (!conversationId) return;
  if (state.liveWatch && state.liveWatch.conversationId === conversationId) return;
  stopLiveWatch();
  const watcher = { conversationId, controller: new AbortController(), cursor: 0 };
  state.liveWatch = watcher;
  const appendProactive = (payload) => {
    const eventId = String(payload.event_id || '');
    if (eventId && state.seenProactive.has(eventId)) return;
    if (eventId) state.seenProactive.add(eventId);
    const text = String(payload.text || '').trim();
    if (!text || !state.conversation || state.conversation.id !== conversationId) return;
    const messages = $('#messages');
    if (!messages) return;
    appendMessage('assistant', text, { time: payload.recorded_at, animate: true });
    scrollToEnd(true);
    logEvent('chat', '收到主动消息', '来自 ' + (state.personaName || '她'), { generation_id: payload.event_id });
  };
  const run = async () => {
    while (state.liveWatch === watcher && !watcher.controller.signal.aborted) {
      try {
        const path = '/api/v1/conversations/' + encodeURIComponent(conversationId) + '/live?after=' + watcher.cursor;
        await client.stream(path, {
          signal: watcher.controller.signal,
          onEvent: (event) => {
            const sequence = Number((event.data && event.data.sequence) || event.id || 0);
            if (sequence > watcher.cursor) watcher.cursor = sequence;
            if (event.type !== 'proactive.message') return;
            const payload = (event.data && event.data.payload) || {};
            appendProactive(payload);
          },
        });
      } catch (error) {
        if (watcher.controller.signal.aborted) return;
      }
      if (state.liveWatch !== watcher) return;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  };
  run();
}

async function loadChatPage(force = false) {  if (!state.conversation) await loadActiveConversation();
  else if (force) await loadHistory();
  requestAnimationFrame(() => {
    if (state.currentPage === 'chat') scrollToEnd(true);
  });
}

async function loadConnectionPage() {
  fillConnectionForm();
  await refreshService();
  try {
    const capabilities = await client.capabilities();
    state.archiveFormat = capabilities.archive_format_version;
    $('#metric-archive-format').textContent = 'v' + capabilities.archive_format_version;
  } catch {
    $('#metric-archive-format').textContent = '—';
  }
}

/* ---------- 启动 / 连接 ---------- */
async function boot({ skipAuth = false } = {}) {
  if (!skipAuth) {
    const saved = (() => { try { return localStorage.getItem(SERVER_STORAGE) || ''; } catch { return ''; } })();
    if (saved && saved !== location.origin) {
      state.serverMode = true;
      state.serverAddress = saved;
      state.serverKey = DEFAULT_KEY;
      client.setBase(saved);
      client.useBearer(DEFAULT_KEY);
    }
    let session = null;
    let connectError = '';
    try { session = await client.session(); } catch (error) { connectError = error.message; }
    if (!session && !state.serverMode && !connectError) {
      try { session = await client.bootstrap(); } catch { session = null; }
    }
    if (!session) {
      showGate(connectError
        ? '无法连接服务器：' + connectError + '。请检查服务器地址与密钥。'
        : (state.serverMode ? '服务器需要客户端密钥，请确认地址和密钥。' : '本机自动登录不可用，请填写服务器地址和客户端密钥。'));
      return false;
    }
  }
  showApp();
  renderPage(pageFromHash(), { load: false });
  fillConnectionForm();
  await refreshService();
  await loadCompanions();
  await loadModelChip();
  await loadVoiceSettings();
  updateVoiceControls();
  await loadArchive().catch(() => {});
  renderPage(pageFromHash(), { load: true });
  logEvent('system', '客户端已连接', '服务' + (state.readyStatus || '未知'), {
    ready: state.readyStatus,
    page: state.currentPage,
    gateway: location.origin,
  });
  return true;
}

async function connectToServer(address, key) {
  let input = (address || '').trim();
  if (!input) input = location.origin;
  if (!/^https?:\/\//i.test(input)) input = 'http://' + input;
  let parsed;
  try { parsed = new URL(input); } catch { throw new Error('服务器地址格式不正确'); }
  const target = parsed.origin + parsed.pathname.replace(/\/$/, '');
  const sameOrigin = parsed.origin === location.origin;
  if (sameOrigin) {
    if (!key) throw new Error('请填写客户端密钥');
    client.setBase('');
    client.clearBearer();
    try { await client.keySession(key); }
    catch (error) {
      if (error.status === 401) throw new Error('客户端密钥不正确');
      throw error;
    }
    state.serverMode = false;
    state.serverAddress = '';
    state.serverKey = key;
    try { localStorage.removeItem(SERVER_STORAGE); } catch {}
  } else {
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('服务器地址需要 http:// 或 https://');
    client.setBase(target);
    client.useBearer(key || DEFAULT_KEY);
    await client.request('/health/live');
    try { await client.session(); }
    catch (error) {
      if (error.status === 401) throw new Error('客户端密钥不正确');
      throw error;
    }
    state.serverMode = true;
    state.serverAddress = target;
    state.serverKey = key || DEFAULT_KEY;
    try { localStorage.setItem(SERVER_STORAGE, target); } catch {}
  }
  state.conversation = null;
  state.persona = null;
  await boot({ skipAuth: true });
  return target;
}

function fillConnectionForm() {
  if (!$('#server-address')) return;
  $('#server-address').value = state.serverAddress || location.origin;
  $('#server-key').value = state.serverKey || DEFAULT_KEY;
  $('#disconnect').hidden = !state.serverMode;
  $('#connection-state').textContent = state.serverMode
    ? '当前：独立客户端（Bearer 密钥）'
    : '当前：同源浏览器会话';
  const facts = $('#connection-facts');
  facts.replaceChildren();
  const rows = [
    ['连接方式', state.serverMode ? '独立客户端' : '同源浏览器'],
    ['服务器地址', state.serverMode ? state.serverAddress : location.origin],
    ['本页地址', location.origin],
    ['服务状态', state.readyStatus || '未知'],
  ];
  for (const [term, value] of rows) facts.append(el('dt', { text: term }), el('dd', { text: value }));
}

/* ---------- 服务状态 ---------- */
async function refreshService() {
  let online = false;
  try {
    const report = await client.request('/health/ready');
    const deps = report.dependencies || {};
    state.readyDeps = deps;
    state.readyStatus = report.status === 'ready' ? '就绪' : '部分可用';
    online = true;
    $('#metric-dialogue').textContent = deps.dialogue === 'configured' ? '已配置' : '待配置';
  } catch {
    state.readyDeps = {};
    state.readyStatus = '无法连接';
    online = false;
    $('#metric-dialogue').textContent = '—';
  }
  const wasOnline = state.lastServiceOnline;
  state.lastServiceOnline = online;
  if (wasOnline !== undefined && wasOnline !== online) {
    logEvent(online ? 'system' : 'error', online ? '服务已恢复' : '服务连接中断', online ? '服务' + state.readyStatus : '健康检查失败');
  }
  const badge = $('#global-status');
  badge.classList.toggle('online', online);
  badge.classList.toggle('offline', !online);
  $('#global-status-text').textContent = online ? '服务' + state.readyStatus : '服务离线';
  $('#sidebar-dot').classList.toggle('online', online);
  $('#sidebar-dot').classList.toggle('offline', !online);
  $('#sidebar-status').textContent = online ? '本机 · 服务' + state.readyStatus : '本机 · 无法连接';
  $('#metric-service').textContent = online
    ? (state.readyStatus === '就绪' ? '就绪' : '部分可用')
    : '离线';
  $('#metric-database').textContent = state.readyDeps.database === 'available' ? '可用' : (online ? '不可用' : '—');
  if (state.currentPage === 'connection') fillConnectionForm();
}

/* ---------- 人格 ---------- */
const PERSONA_CORE_FIELDS = ['name', 'tagline', 'user_name', 'description', 'avatar_asset_id', 'user_avatar_asset_id'];
const PERSONA_STYLE_FIELDS = ['tone', 'habits', 'reply_length'];

function collectFields(container) {
  const result = {};
  for (const row of container.querySelectorAll('.kv')) {
    const key = row.querySelector('.kv-key').value.trim();
    if (!key) continue;
    const raw = row.querySelector('.kv-value').value;
    try { result[key] = JSON.parse(raw); } catch { result[key] = raw; }
  }
  return result;
}

function kvRow(element, key = '', value = '') {
  const row = el('div', { class: 'kv' });
  const head = el('div', { class: 'kv-head' }, [
    el('input', { class: 'input small kv-key', placeholder: '字段名', value: key }),
    el('button', { class: 'icon-btn', type: 'button', title: '删除字段', text: '✕', onclick: () => row.remove() }),
  ]);
  const body = el('textarea', { class: 'input kv-value', rows: '2', placeholder: '字段值（可写 JSON）' });
  body.value = value;
  row.append(head, body);
  element.append(row);
  return row;
}

function renderExtras(container, data) {
  container.replaceChildren();
  for (const [key, value] of Object.entries(data || {})) {
    kvRow(container, key, typeof value === 'string' ? value : JSON.stringify(value, null, 2));
  }
  container.append(el('button', {
    class: 'btn ghost small', type: 'button', text: '＋ 添加字段',
    onclick: () => kvRow(container, '', ''),
  }));
}

function splitPersona(persona) {
  const extraCore = {};
  const extraStyle = {};
  for (const [key, value] of Object.entries(persona.core || {})) {
    if (!PERSONA_CORE_FIELDS.includes(key)) extraCore[key] = value;
  }
  for (const [key, value] of Object.entries(persona.style || {})) {
    if (!PERSONA_STYLE_FIELDS.includes(key)) extraStyle[key] = value;
  }
  return { extraCore, extraStyle };
}

function detectLegacy(persona) {
  const core = persona.core || {};
  if (core.name || core.description) return null;
  const entries = Object.entries(core).filter(([, value]) => typeof value === 'string' && value.trim().length >= 120);
  if (!entries.length) return null;
  return { key: entries[0][0], value: entries[0][1] };
}

function updatePersonaCard() {
  const name = $('#persona-name').value.trim();
  const tagline = $('#persona-tagline').value.trim();
  const description = $('#persona-description').value.trim().replace(/\s+/g, ' ');
  const displayName = name || (state.persona && state.persona._legacyName) || '未命名人格';
  $('#persona-name-display').textContent = displayName;
  paintAvatar($('#persona-avatar'), displayName, state.avatarPending === undefined ? state.avatarAssetId : state.avatarPending);
  $('#persona-tagline-display').textContent = tagline || description.slice(0, 120) || '还没有填写人物设定。';
}

async function loadPersona() {
  if (!state.companionId) return;
  try {
    const persona = await client.persona(state.companionId);
    state.persona = persona;
    const legacy = detectLegacy(persona);
    const core = persona.core || {};
    const style = persona.style || {};
    const { extraCore, extraStyle } = splitPersona(persona);
    if (legacy) { persona._legacyName = legacy.key; persona._legacyValue = legacy.value; }
    $('#persona-card-fields').textContent = String(Object.keys(core).length + Object.keys(style).length);
    $('#persona-card-companion').textContent = state.companionName || '—';
    state.personaName = core.name || (legacy ? legacy.key : '');
    state.userName = core.user_name || '';
    state.avatarAssetId = core.avatar_asset_id || null;
    state.avatarPending = undefined;
    state.userAvatarAssetId=core.user_avatar_asset_id||null;
    state.userAvatarPending=undefined;
    paintAvatar($('#user-avatar'),state.userName||'我',state.userAvatarAssetId);
    $('#user-avatar-remove').hidden=!state.userAvatarAssetId;
    paintAvatar($('#persona-avatar'), state.personaName, state.avatarAssetId);
    $('#persona-avatar-remove').hidden = !state.avatarAssetId;
    updateChatIdentity();
    $('#persona-name').value = core.name || (legacy ? legacy.key : '');
    $('#persona-user-name').value = core.user_name || '';
    $('#persona-tagline').value = core.tagline || '';
    $('#persona-description').value = core.description || (legacy ? legacy.value : '');
    $('#persona-tone').value = style.tone || '';
    $('#persona-habits').value = style.habits || '';
    $('#persona-length').value = style.reply_length || '';
    renderExtras($('#persona-core-raw'), extraCore);
    renderExtras($('#persona-style-raw'), extraStyle);
    $('#persona-migrate').hidden = !legacy;
    if (legacy) {
      $('#persona-migrate-hint').textContent = '现有数据是“' + legacy.key + '”一个键里放着全部设定。可以一键整理成结构化字段，原始内容不会丢失，保存后才写入新版本。';
    }
    state.personaBytes = new TextEncoder().encode(JSON.stringify({ core, style })).length;
    $('#persona-budget').textContent = '约 ' + (state.personaBytes / 1024).toFixed(1) + ' KB / 48 KB';
    const longExtras = Object.entries(extraCore).filter(([, value]) => typeof value === 'string' && value.length > 600).map(([key]) => key);
    const duplicated = longExtras.length > 0 && String(core.description || '').length > 600;
    const warning = duplicated || longExtras.length > 0 || state.personaBytes > 40000;
    $('#persona-size').hidden = !warning;
    if (warning) {
      $('#persona-size-hint').textContent = duplicated
        ? '“她的一生”之外还保留了较长的旧字段（' + longExtras.join('、') + '），保存后同一段设定会重复进入上下文。建议在“高级：原始字段”里删除旧的长文本字段。'
        : longExtras.length
          ? '高级字段里有较长文本（' + longExtras.join('、') + '）。如果它们是重复的人物设定，建议删除。'
          : '档案约 ' + (state.personaBytes / 1024).toFixed(1) + ' KB，接近 48 KB 的上下文预算，可以适当精简。';
    }
    $('#persona-state').textContent = '';
    updatePersonaCard();
  } catch (error) {
    $('#persona-state').textContent = '读取人格失败：' + error.message;
  }
}

function normalizePersona() {
  if (!state.persona || !state.persona._legacyName) return;
  const legacyKey = state.persona._legacyName;
  $('#persona-name').value = legacyKey;
  $('#persona-description').value = state.persona._legacyValue || '';
  for (const row of $$('#persona-core-raw .kv')) {
    const input = row.querySelector('.kv-key');
    if (input && input.value.trim() === legacyKey) row.remove();
  }
  $('#persona-migrate').hidden = true;
  updatePersonaCard();
  $('#persona-state').textContent = '已整理到表单；点右上角“保存档案”后才会写入新版本。';
}

async function savePersona() {
  if (!state.persona) return;
  const core = collectFields($('#persona-core-raw'));
  const style = collectFields($('#persona-style-raw'));
  const name = $('#persona-name').value.trim();
  const userName = $('#persona-user-name').value.trim();
  const tagline = $('#persona-tagline').value.trim();
  const description = $('#persona-description').value.trim();
  const tone = $('#persona-tone').value.trim();
  const habits = $('#persona-habits').value.trim();
  const length = $('#persona-length').value;
  if (name) core.name = name;
  if (userName) core.user_name = userName;
  if (tagline) core.tagline = tagline;
  if (description) core.description = description;
  if (tone) style.tone = tone;
  if (habits) style.habits = habits;
  if (length) style.reply_length = length;
  const avatarAssetId = state.avatarPending === undefined ? state.avatarAssetId : state.avatarPending;
  if (avatarAssetId) core.avatar_asset_id = avatarAssetId;
  const userAvatarAssetId=state.userAvatarPending===undefined?state.userAvatarAssetId:state.userAvatarPending;
  if(userAvatarAssetId)core.user_avatar_asset_id=userAvatarAssetId;
  if (!name && !description) {
    $('#persona-state').textContent = '至少填写人格名称或人物设定，空人格不会保存。';
    return;
  }
  const longExtras = Object.entries(core).filter(([key, value]) => key !== 'description' && typeof value === 'string' && value.length > 600);
  if (longExtras.length && description.length > 600) {
    const proceed = await openModal({
      title: '档案里有重复的长文本',
      hint: '检测到 ' + longExtras.map(([key]) => key).join('、') + ' 与“她的一生”都很长，保存后同一段设定会重复占用上下文。是否仍然保存？',
      okText: '仍然保存',
    });
    if (!proceed) return;
  }
  try {
    const updated = await client.patchPersona(state.companionId, state.persona.version, { core, style });
    state.persona = updated;
    state.avatarPending = undefined;
    toast('人格已保存。');
    logEvent('system', '灵魂档案已保存', '人格与风格已写入新版本');
    await loadPersona();
    $('#persona-state').textContent = '已保存；正在进行的回答会按新版本收敛。';
  } catch (error) {
    if (error.code === 'PERSONA_CONFLICT') {
      $('#persona-state').textContent = '版本冲突：别处已修改人格，已重新加载，请核对后重试。';
      await loadPersona();
    } else {
      $('#persona-state').textContent = '保存失败：' + error.message;
    }
  }
}

/* ---------- 消息 ---------- */
async function paintAvatar(node, name, assetId) {
  const token=Symbol('avatar');node._avatarToken=token;
  node.replaceChildren();
  node.classList.toggle('has-image', Boolean(assetId));
  if (assetId) {
    try {
      const src = await client.assetObjectUrl(assetId);
      if(node._avatarToken!==token)return;
      node.replaceChildren(el('img', { src, alt: name || '头像' }));
      return;
    } catch {
      if(node._avatarToken!==token)return;
      node.classList.remove('has-image');
    }
  }
  node.textContent = (name || 'W').slice(0, 1).toUpperCase();
}

async function updateLogAvatar() {
  const assetId = state.avatarPending === undefined ? state.avatarAssetId : state.avatarPending;
  const medallion = $('#log-avatar');
  if (medallion) paintAvatar(medallion, state.personaName || '她', assetId);
  const drawer = $('#log-drawer');
  if (!drawer) return;
  if (!assetId) { drawer.style.removeProperty('--log-avatar-image'); return; }
  try {
    const url = await client.assetObjectUrl(assetId);
    drawer.style.setProperty('--log-avatar-image', 'url("' + url + '")');
  } catch {
    drawer.style.removeProperty('--log-avatar-image');
  }
}

function refreshMessageIdentity() {
  $$('#messages .msg').forEach((node) => {
    const assistant = node.getAttribute('data-role') === 'assistant';
    const avatar = node.querySelector('.avatar');
    const nameEl = node.querySelector('.msg-name');
    if (avatar) paintAvatar(avatar, assistant ? (state.personaName || 'W') : (state.userName || '我'), assistant ? state.avatarAssetId : state.userAvatarAssetId);
    if (nameEl) nameEl.textContent = assistant ? (state.personaName || 'AGI.Wind') : state.userName;
  });
}

function updateChatIdentity() {
  const name = state.personaName || state.companionName || '她';
  $('#empty-title').textContent = name ? '开始和 ' + name + ' 说话' : '开始一段新的对话';
  paintAvatar($('#empty-avatar'), name, state.avatarAssetId);
  updateLogAvatar();
  renderCurrentConversation();
  refreshMessageIdentity();
}

function appendChatTime(container, value) {
  const date = value ? new Date(value) : new Date();
  const time = date.getTime();
  if (!Number.isFinite(time)) return;
  const previous = Number(container.dataset.lastMessageAt);
  const previousDate = new Date(previous);
  if (!container.dataset.lastMessageAt || time - previous >= 5 * 60 * 1000 || date.toDateString() !== previousDate.toDateString()) {
    const now = new Date();
    const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
    const clock = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
    const day = date.toDateString() === now.toDateString() ? ''
      : date.toDateString() === yesterday.toDateString() ? '昨天 '
      : (date.getFullYear() !== now.getFullYear() ? date.getFullYear() + '年' : '') + (date.getMonth() + 1) + '月' + date.getDate() + '日 ';
    container.append(el('time', { class: 'chat-time', datetime: date.toISOString(), text: day + clock }));
  }
  container.dataset.lastMessageAt = String(time);
}

function messageNode(role, text, options = {}) {
  const assistant = role === 'assistant';
  const node = el('article', { class: 'msg' + (options.className ? ' ' + options.className : '') + (options.animate ? ' animate-in' : ''), 'data-role': role });
  const avatar = el('div', { class: 'avatar' });
  paintAvatar(avatar, assistant ? (state.personaName || 'W') : (state.userName || '我'), assistant ? state.avatarAssetId : state.userAvatarAssetId);
  const name = assistant ? (state.personaName || 'AGI.Wind') : state.userName;
  const meta = el('div', { class: 'msg-head' });
  if (name) meta.append(el('span', { class: 'msg-name', text: name }));
  const body = el('div', { class: 'text' });
  if (options.markdown) body.innerHTML = renderMarkdown(text);
  else body.textContent = text;
  const bubble = el('div', { class: 'bubble' }, [body]);
  const bodyWrap = el('div', { class: 'msg-body' }, [meta, bubble]);
  node.append(avatar, bodyWrap);
  const message={ node, bubble, body, meta };
  if(options.attachments?.length)messageImages(client,message,options.attachments);
  return message;
}

function appendMessage(role, text, options = {}) {
  const message = messageNode(role, text, options);
  appendChatTime($('#messages'), options.time);
  $('#messages').append(message.node);
  scrollToEnd();
  return message;
}

function createReplyTurn(container,options={},first=null) {
  if (!first) appendChatTime(container, options.time);
  const anchor=document.createComment('reply end');container.append(anchor);
  const texts=first?[first]:[],images=new Map(),voices=new Map(),voiceItems=[];
  const animate=options.animate===true;
  const reducedMotion=typeof window.matchMedia==='function'&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const segmentDelay=reducedMotion?0:(options.segmentDelay??700);
  const add=()=>{const item=messageNode('assistant','',{...options,animate});container.insertBefore(item.node,anchor);return item;};
  const revealQueue=[];let revealTimer=null;
  const revealNow=(item)=>{if(!item.node.isConnected)return;item.queued=false;item.node.hidden=false;item.node.classList.remove('animate-in');void item.node.offsetWidth;item.node.classList.add('animate-in');if(animate)scrollToEnd(true);};
  const drain=()=>{revealTimer=null;while(revealQueue.length){const item=revealQueue.shift();if(!item.node.isConnected)continue;revealNow(item);break;}if(revealQueue.length)revealTimer=setTimeout(drain,segmentDelay);};
  const queueReveal=(item)=>{if(!animate||segmentDelay<=0){item.node.hidden=false;return;}item.node.hidden=true;item.queued=true;revealQueue.push(item);if(revealTimer===null)revealTimer=setTimeout(drain,segmentDelay);};
  const all=()=>texts.concat([...images.values()],voiceItems);
  const voice=(voiceOptions={})=>{
    const key=String(voiceOptions.segment_id||voiceOptions.attachment?.segment_id||voiceOptions.attachment?.asset_id||'speech');
    let item=voices.get(key);
    if(!item){
      item=add();
      item.node.dataset.messageKind='audio';
      item.body.hidden=true;
      item.audio=createAudioMessage(client,item,voiceOptions);
      voices.set(key,item);
      voiceItems.push(item);
      if(!texts.some((text)=>text.node.isConnected)&&!images.size)item.node.hidden=false;
      else queueReveal(item);
      if(animate)scrollToEnd(true);
    }else{
      item.audio.update(voiceOptions);
    }
    return item;
  };
  const turn={
    update(text,attachments=[],streaming=false){
      const imageAttachments=attachments.filter(isImageAttachment);
      const audioAttachments=attachments.filter(isAudioAttachment);
      let parts=splitReply(text,{final:!streaming});
      if(!parts.length&&!attachments.length)parts=[{text:'',complete:!streaming}];
      const start=texts.length;
      for(let i=start;i<parts.length;i++){
        const item=add();texts[i]=item;
        if(i===0&&!texts.some((text)=>text.node.isConnected))item.node.hidden=false;else queueReveal(item);
      }
      for(let i=0;i<parts.length;i++){
        const item=texts[i]||(texts[i]=add());const part=parts[i];
        item.node.dataset.messageKind='text';
        if(!item.queued)item.node.hidden=false;
        const complete=part.complete||!streaming;
        if(item.partText!==part.text||item.partComplete!==complete){
          item.partText=part.text;item.partComplete=complete;
          if(complete)item.body.innerHTML=renderMarkdown(part.text);
          else item.body.textContent=part.text;
        }
        item.node.classList.toggle('pending',streaming&&!complete);
        item.node.classList.toggle('streaming',streaming&&!complete);
      }
      while(texts.length>parts.length)texts.pop().node.remove();
      for(const attachment of imageAttachments){
        if(images.has(attachment.asset_id))continue;
        const item=add();item.node.dataset.messageKind='image';item.body.hidden=true;
        if(!texts.some((text)=>text.node.isConnected)&&!images.size)item.node.hidden=false;else queueReveal(item);
        messageImages(client,item,[attachment]);images.set(attachment.asset_id,item);
      }
      for(const attachment of audioAttachments){
        voice({
          state:'ready',
          attachment,
          segment_id:attachment.segment_id,
          voice_id:attachment.voice_id,
          emotion:attachment.emotion,
          autoplay:false,
        });
      }
      return turn;
    },
    voice,
    last(){return texts.at(-1)||[...images.values()].at(-1)||first;},
    settle(){for(const item of all())item.node.classList.remove('pending','streaming');},
  };
  return turn;
}

const LIVE_TYPING = { baseMs: 220, perCharMs: 55, punctMs: 120, gapMs: 320, maxMs: 12000 };

function liveTypingConfig() {
  const override = typeof window !== 'undefined' && window.__AGI_TYPING__ ? window.__AGI_TYPING__ : {};
  return Object.assign({}, LIVE_TYPING, override);
}

function liveSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

function createLiveReplyTurn(container, options = {}, first = null) {
  if (!first) appendChatTime(container, options.time);
  const anchor = document.createComment('live reply end');
  container.append(anchor);
  const animate = options.animate === true;
  const reduceMotion = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const timing = liveTypingConfig();
  const jobs = [];
  const voiceEntries = new Map();
  const earlyVoices = new Map();
  const pendingImages = [];
  const seenImages = new Set();
  let firstPending = first || null;
  let pumping = false;
  let finished = false;
  let abortState = '';
  let lastItem = null;
  let autoPlayed = false;

  const addItem = (kind) => {
    let base = firstPending;
    firstPending = null;
    if (!base) {
      base = messageNode('assistant', '', { animate: false });
      container.insertBefore(base.node, anchor);
    }
    base.node.hidden = true;
    base.node.classList.remove('pending', 'streaming');
    base.node.dataset.messageKind = kind;
    const item = { node: base.node, bubble: base.bubble, body: base.body, meta: base.meta, kind, partText: '' };
    lastItem = item;
    return item;
  };

  async function typeItem(item, text) {
    const value = stripHiddenReplyPrefix(text);
    item.kind = 'text';
    item.node.dataset.messageKind = 'text';
    item.node.hidden = false;
    item.node.classList.remove('voice-pending', 'voice-fallback');
    item.body.hidden = false;
    if (!animate || reduceMotion || abortState) {
      item.body.innerHTML = renderMarkdown(value);
      item.partText = value;
      scrollToEnd(true);
      return;
    }
    item.node.classList.add('typing');
    let shown = '';
    for (const char of Array.from(value)) {
      shown += char;
      item.body.textContent = shown;
      scrollToEnd();
      await liveSleep(timing.perCharMs + ('。！？!?，、；：…—'.includes(char) ? timing.punctMs : 0));
      if (abortState) break;
    }
    item.body.innerHTML = renderMarkdown(value);
    item.partText = value;
    item.node.classList.remove('typing');
    await liveSleep(timing.gapMs);
    scrollToEnd();
  }

  function settleVoice(entry, state, data = {}) {
    if (!entry || entry.state) return;
    entry.state = state;
    entry.data = data;
    if (entry.resolve) entry.resolve(state);
  }

  async function deliverVoice(entry) {
    const item = entry.item;
    item.node.hidden = false;
    item.node.dataset.messageKind = 'audio';
    item.body.hidden = true;
    item.node.classList.add('voice-pending');
    item.audio.update({ state: 'queued', segment_id: entry.segment_id });
    scrollToEnd(true);
    const state = await entry.wait;
    item.node.classList.remove('voice-pending');
    if (state === 'ready') {
      const autoplay = !autoPlayed;
      autoPlayed = true;
      item.audio.update({ state: 'ready', attachment: entry.data.attachment, segment_id: entry.segment_id, autoplay });
      scrollToEnd(true);
      await liveSleep(260);
      return;
    }
    item.kind = 'text';
    item.node.dataset.messageKind = 'text';
    item.node.classList.remove('voice-pending');
    if (item.audio) {
      try { item.audio.destroy(); } catch {}
      item.audio = null;
    }
    item.body.hidden = false;
    item.body.innerHTML = renderMarkdown(entry.text);
    item.node.classList.add('voice-fallback');
    item.meta.append(el('span', { class: 'meta', text: state === 'cancelled' ? '语音已取消，保留文字' : '语音生成失败，保留文字' }));
    scrollToEnd(true);
  }

  function revealImages() {
    for (const attachment of pendingImages) {
      if (!attachment || seenImages.has(attachment.asset_id)) continue;
      seenImages.add(attachment.asset_id);
      const item = addItem('image');
      item.node.hidden = false;
      item.body.hidden = true;
      messageImages(client, item, [attachment]);
      scrollToEnd(true);
    }
    pendingImages.length = 0;
  }

  async function pump() {
    if (pumping) return;
    pumping = true;
    try {
      while (jobs.length) {
        const job = jobs.shift();
        if (abortState) {
          if (job.type === 'text') {
            job.item.node.hidden = false;
            job.item.body.innerHTML = renderMarkdown(job.text || '');
          } else if (job.type === 'voice') {
            job.item.node.hidden = false;
            job.item.node.dataset.messageKind = 'text';
            job.item.node.classList.remove('voice-pending');
            if (job.item.audio) {
              try { job.item.audio.destroy(); } catch {}
              job.item.audio = null;
            }
            job.item.body.hidden = false;
            job.item.body.innerHTML = renderMarkdown(job.entry.text || '');
          }
          continue;
        }
        if (job.type === 'text') await typeItem(job.item, job.text);
        else if (job.type === 'voice') await deliverVoice(job.entry);
      }
      if (finished) revealImages();
    } finally {
      pumping = false;
      if (jobs.length && !pumping) pump();
    }
  }

  function consumeEarlyVoice(segmentId, entry) {
    const early = earlyVoices.get(segmentId);
    if (!early) return;
    earlyVoices.delete(segmentId);
    settleVoice(entry, early.state, early.data || {});
  }

  const controller = {
    segment(payload = {}) {
      const text = String(payload.text || '');
      if (!text) return;
      if (payload.kind === 'voice' && payload.voice && payload.voice.segment_id) {
        const segmentId = String(payload.voice.segment_id);
        const item = addItem('audio');
        item.node.dataset.messageKind = 'audio';
        item.body.hidden = true;
        item.audio = createAudioMessage(client, item, { state: 'queued', segment_id: segmentId });
        const entry = { segment_id: segmentId, text, item, state: '', data: {} };
        entry.wait = new Promise((resolve) => { entry.resolve = resolve; });
        voiceEntries.set(segmentId, entry);
        if (payload.voice.state === 'failed' || payload.voice.state === 'cancelled') {
          settleVoice(entry, payload.voice.state, payload.voice);
        }
        consumeEarlyVoice(segmentId, entry);
        jobs.push({ type: 'voice', entry });
      } else {
        const item = addItem('text');
        item.node.hidden = true;
        jobs.push({ type: 'text', item, text });
      }
      pump();
    },
    voiceState(data = {}) {
      const segmentId = String(data.segment_id || '');
      if (!segmentId) return;
      const entry = voiceEntries.get(segmentId);
      const state = data.state || '';
      const value = state === 'skipped' ? 'failed' : state;
      if (!entry) {
        earlyVoices.set(segmentId, { state: value, data });
        return;
      }
      if (state === 'ready' || state === 'failed' || state === 'cancelled' || state === 'skipped') {
        settleVoice(entry, value, data);
      }
    },
    sticker(attachment) {
      if (attachment) pendingImages.push(attachment);
    },
    finish(state = 'completed') {
      if (finished) return;
      finished = true;
      abortState = state === 'failed' || state === 'cancelled' ? state : '';
      if (abortState) {
        for (const entry of voiceEntries.values()) settleVoice(entry, abortState, {});
      }
      pump();
    },
    update(text, attachments = []) {
      const value = String(text || '');
      if (value) {
        const item = addItem('text');
        item.node.hidden = false;
        item.body.innerHTML = renderMarkdown(value);
        item.partText = value;
      }
      for (const attachment of attachments) controller.sticker(attachment);
      finished = true;
      abortState = abortState || 'failed';
      revealImages();
      scrollToEnd(true);
    },
    voice(voiceOptions = {}) { controller.voiceState(voiceOptions); },
    last() { return lastItem || firstPending; },
    settle() { if (lastItem) lastItem.node.classList.remove('pending', 'streaming'); },
    pendingVoiceIds() {
      return [...voiceEntries.values()].filter((entry) => !entry.state).map((entry) => entry.segment_id);
    },
    hasVoiceSegments() { return voiceEntries.size > 0; },
  };
  return controller;
}

function startVoiceWatchSegments(generationId, controller) {
  if (!generationId || !controller) return;
  const key = 'segments:' + generationId;
  if (state.voiceWatchers.has(key)) return;
  const watcher = { stopped: false, timer: null };
  state.voiceWatchers.set(key, watcher);
  const startedAt = Date.now();
  let emptyPolls = 0;
  const finish = () => {
    watcher.stopped = true;
    if (watcher.timer) clearTimeout(watcher.timer);
    state.voiceWatchers.delete(key);
  };
  const failPending = (code) => {
    for (const segmentId of controller.pendingVoiceIds()) {
      controller.voiceState({ segment_id: segmentId, state: 'failed', error_code: code });
    }
    finish();
  };
  const tick = async () => {
    if (watcher.stopped) return;
    if (Date.now() - startedAt > 120000) { failPending('TIMEOUT'); return; }
    try {
      const data = await client.generationSpeech(generationId);
      const segments = Array.isArray(data && data.segments) ? data.segments : [];
      for (const segment of segments) controller.voiceState(segment);
      if (!segments.length && data && data.state && data.state !== 'none') {
        controller.voiceState({ ...data, segment_id: data.segment_id || '' });
      }
      emptyPolls = segments.length ? 0 : emptyPolls + 1;
      const pending = segments.filter((segment) => segment.state === 'queued' || segment.state === 'synthesizing');
      if (!pending.length && controller.pendingVoiceIds().length === 0) { finish(); return; }
      if (!segments.length && emptyPolls >= 6) { failPending('TTS_UNAVAILABLE'); return; }
      watcher.timer = setTimeout(tick, 700);
    } catch {
      watcher.timer = setTimeout(tick, 700);
    }
  };
  tick();
}

function renderHistorySegments(container, event) {
  const content = event.content || {};
  const attachments = event.attachments || content.attachments || [];
  const segments = Array.isArray(content.segments) ? content.segments : [];
  const voices = new Map((content.voices || []).map((entry) => [String(entry.segment_id), entry]));
  const audios = attachments.filter(isAudioAttachment);
  const images = attachments.filter((attachment) => !isAudioAttachment(attachment));
  appendChatTime(container, event.recorded_at);
  const pending = new Map();
  for (const segment of segments) {
    const segmentText = stripHiddenReplyPrefix(segment.text || '');
    if (!segmentText && segment.kind !== 'voice') continue;
    if (segment.kind === 'voice' && segment.segment_id) {
      const segmentId = String(segment.segment_id);
      const info = voices.get(segmentId) || {};
      const audio = audios.find((item) => String(item.segment_id || '') === segmentId);
      const message = messageNode('assistant', '', {});
      container.append(message.node);
      message.node.dataset.messageKind = 'audio';
      message.body.hidden = true;
      if (audio) {
        createAudioMessage(client, message, { state: 'ready', attachment: audio, segment_id: segmentId, autoplay: false });
      } else if (info.state === 'queued' || info.state === 'synthesizing') {
        const player = createAudioMessage(client, message, { state: info.state, segment_id: segmentId, autoplay: false });
        pending.set(segmentId, { message, player, text: segmentText });
      } else {
        message.node.dataset.messageKind = 'text';
        message.body.hidden = false;
        message.body.innerHTML = renderMarkdown(segmentText);
        message.meta.append(el('span', { class: 'meta', text: '语音不可用，显示文字' }));
      }
    } else {
      const message = messageNode('assistant', '', {});
      message.body.innerHTML = renderMarkdown(segmentText);
      container.append(message.node);
    }
  }
  for (const attachment of images) {
    if (!attachment) continue;
    const message = messageNode('assistant', '', {});
    container.append(message.node);
    message.node.dataset.messageKind = 'image';
    message.body.hidden = true;
    messageImages(client, message, [attachment]);
  }
  if (pending.size) {
    const watcher = {
      pendingVoiceIds: () => [...pending.keys()],
      voiceState(data = {}) {
        const segmentId = String(data.segment_id || '');
        const entry = pending.get(segmentId);
        if (!entry) return;
        if (data.state === 'ready') {
          entry.player.update({ state: 'ready', attachment: data.attachment, segment_id: segmentId, autoplay: false });
          pending.delete(segmentId);
        } else if (data.state === 'failed' || data.state === 'cancelled' || data.state === 'skipped') {
          entry.message.node.dataset.messageKind = 'text';
          entry.message.body.hidden = false;
          entry.message.body.innerHTML = renderMarkdown(entry.text);
          entry.message.meta.append(el('span', { class: 'meta', text: '语音不可用，显示文字' }));
          pending.delete(segmentId);
        }
      },
    };
    startVoiceWatchSegments(event.generation_id, watcher);
  }
  scrollToEnd(true);
}

function scrollToEnd(force = false) {
  const box = $('#messages');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  if (force) box.scrollTo({ top: box.scrollHeight, behavior: 'instant' });
  else if (near) box.scrollTop = box.scrollHeight;
}

function clearChat() {
  stopLiveWatch();
  stopAllAudio();
  $('#messages').replaceChildren();
  delete $('#messages').dataset.lastMessageAt;
  $('#chat-empty').hidden = false;
}

function showChatEmpty(show) {
  $('#chat-empty').hidden = !show;
}

async function loadHistory() {
  if (!state.conversation) return;
  clearChat();
  try {
    const events = await client.history(state.conversation.id);
    for (const event of events) {
      if(!['user_input','assistant_output'].includes(event.kind))continue;
      if (event.content?.proactive && event.id) state.seenProactive.add(String(event.id));
      logHistoryProcessing(event);
      const text = stripHiddenReplyPrefix(event.content && (event.content.text || ''));
      const attachments=event.attachments||event.content?.attachments||[];
      if (!text&&!attachments.length) continue;
      const role = event.role === 'assistant' ? 'assistant' : 'user';
      if(role==='assistant'){
        const segments=event.content?.segments;
        if(Array.isArray(segments)&&segments.length)renderHistorySegments($('#messages'),event);
        else{const turn=createReplyTurn($('#messages'),{time:event.recorded_at}).update(text,attachments);handleHistoryVoice(event,turn);}
      }
      else appendMessage(role,text,{time:event.recorded_at,attachments});
    }
    showChatEmpty($('#messages').children.length === 0);
    scrollToEnd(true);
    startLiveWatch(state.conversation.id);
  } catch (error) {
    toast('读取历史失败：' + error.message);
    logEvent('error', '读取历史失败', error.message);
  }
}

/* ---------- 单会话 ---------- */
function defaultConversationTitle() {
  return '与 ' + (state.personaName || state.companionName || '她') + ' 的对话';
}

function renderCurrentConversation() {
  const conversation = state.conversation;
  if (!conversation) return;
  const title = conversation.title || defaultConversationTitle();
  $('#current-chat-title').textContent = title;
  $('#chat-title').textContent = title;
}

async function loadCompanions() {
  try {
    const companions = await client.companions();
    if (!companions.length) { toast('服务端还没有角色，请先运行初始化命令。'); return; }
    state.companionId = companions[0].id;
    state.companionName = companions[0].name || '';
    state.memoryRevision = companions[0].memory_revision || 0;
    await loadPersona().catch(() => {});
    await loadActiveConversation();
  } catch (error) {
    toast('读取角色失败：' + error.message);
  }
}

async function loadActiveConversation() {
  if (!state.companionId) return;
  try {
    let rows = await client.conversations(state.companionId, 'active');
    if (!rows.length) rows = [await client.createConversation(state.companionId, defaultConversationTitle())];
    state.conversation = rows[0];
    renderCurrentConversation();
    await loadHistory();
    $('#empty-title').textContent = state.personaName ? '开始和 ' + state.personaName + ' 说话' : '开始一段新的对话';
  } catch (error) {
    toast('读取会话失败：' + error.message);
  }
}

async function startNewConversation() {
  state.conversation = await client.createConversation(state.companionId, defaultConversationTitle());
  renderCurrentConversation();
  await loadHistory();
}

/* ---------- 对话菜单 ---------- */
function closeSessionMenu() {
  $('#session-menu').hidden = true;
  $('#conversation-menu-button').setAttribute('aria-expanded', 'false');
}

function toggleSessionMenu() {
  const menu = $('#session-menu');
  menu.hidden = !menu.hidden;
  $('#conversation-menu-button').setAttribute('aria-expanded', String(!menu.hidden));
}

async function renameCurrentConversation() {
  closeSessionMenu();
  if (!state.conversation) return;
  const title = await openModal({
    title: '重命名对话',
    hint: '标题只影响显示，不会改变她的身份和聊天记录。',
    input: state.conversation.title || '',
    okText: '保存',
  });
  if (title === null) return;
  try {
    const updated = await client.updateConversation(state.conversation.id, { title });
    state.conversation = { ...state.conversation, ...updated };
    renderCurrentConversation();
    toast('对话已重命名。');
    logEvent('system', '对话已重命名', title || '未命名对话');
  } catch (error) {
    toast('重命名失败：' + error.message);
    logEvent('error', '重命名失败', error.message);
  }
}

async function archiveCurrentConversation() {
  closeSessionMenu();
  if (!state.conversation) return;
  const proceed = await openModal({
    title: '归档当前对话？',
    hint: '这段对话会变为只读档案。新的一段对话仍是同一个她，人格、记忆与关系不会重置。',
    okText: '归档并开始新对话',
  });
  if (!proceed) return;
  try {
    await startNewConversation();
    await loadArchive().catch(() => {});
    toast('已归档，开始新的一段对话。');
    logEvent('system', '对话已归档', '新对话已开始');
  } catch (error) {
    toast('归档失败：' + error.message);
    logEvent('error', '归档失败', error.message);
  }
}

async function deleteCurrentConversation() {
  closeSessionMenu();
  if (!state.conversation) return;
  const proceed = await openModal({
    title: '永久删除当前对话？',
    hint: '会删除这段对话的全部消息、附件引用及由它产生的记忆来源，无法恢复。删除后会开始一段新的空对话。',
    okText: '永久删除',
    danger: true,
  });
  if (!proceed) return;
  const conversationId = state.conversation.id;
  try {
    await client.deleteConversation(conversationId);
    await startNewConversation();
    await loadArchive().catch(() => {});
    toast('当前对话已永久删除，并已开始新对话。');
    logEvent('system', '当前对话已永久删除', '消息与记忆来源已清除', { conversation_id: conversationId });
  } catch (error) {
    toast('删除失败：' + error.message);
    logEvent('error', '永久删除对话失败', error.message);
  }
}

/* ---------- 归档 ---------- */
async function loadArchive() {
  if (!state.companionId) return;
  try {
    const archived = await client.conversations(state.companionId, 'archived');
    const hidden = await client.conversations(state.companionId, 'hidden');
    const rows = archived.concat(hidden);
    rows.sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
    state.archiveRows = rows;
    state.archiveCount = rows.length;
    state.lastArchive = rows.length
      ? rows.map((row) => row.created_at).sort().slice(-1)[0]
      : '';
    renderArchive();
  } catch (error) {
    $('#archive-hint').textContent = '读取归档失败：' + error.message;
    logEvent('error', '读取归档失败', error.message);
  }
}

function updateArchiveMetrics() {
  $('#metric-archive-count').textContent = String(state.archiveCount || 0);
  $('#metric-active-count').textContent = state.conversation ? '1' : '0';
  $('#metric-last-archive').textContent = state.lastArchive ? formatTime(state.lastArchive) : '—';
  $('#nav-archive-count').textContent = String(state.archiveCount || 0);
}

function renderArchive() {
  const list = $('#archive-list');
  list.replaceChildren();
  for (const conversation of state.archiveRows) {
    list.append(el('li', { class: 'archive-item' }, [
      el('div', { class: 'archive-title' }, [
        el('strong', { text: conversation.title || '未命名对话' }),
        el('span', { class: 'meta', text: 'ID ' + String(conversation.id).slice(0, 8) }),
      ]),
      el('div', {}, [el('span', { class: 'pill b', text: '已归档' })]),
      el('div', { class: 'meta', text: formatTime(conversation.created_at) }),
      el('div', { class: 'archive-actions' }, [
        el('button', { class: 'btn ghost small', type: 'button', text: '查看', onclick: () => viewArchive(conversation) }),
        el('button', { class: 'btn ghost small', type: 'button', text: '恢复', onclick: () => restoreArchive(conversation) }),
        el('button', { class: 'btn danger small', type: 'button', text: '删除', onclick: () => deleteArchive(conversation) }),
      ]),
    ]));
  }
  $('#archive-empty').hidden = state.archiveRows.length > 0;
  $('#archive-hint').textContent = state.archiveRows.length
    ? ''
    : '';
  updateArchiveMetrics();
}

async function viewArchive(conversation) {
  try {
    const events = await client.history(conversation.id);
    state.archiveViewId = conversation.id;
    $('#archive-view-title').textContent = conversation.title || '档案回看';
    const box = $('#archive-messages');
    box.replaceChildren();
    delete box.dataset.lastMessageAt;
    for (const event of events) {
      if(!['user_input','assistant_output'].includes(event.kind))continue;
      logHistoryProcessing(event);
      const text = event.content && (event.content.text || '');
      const attachments=event.attachments||event.content?.attachments||[];
      if (!text&&!attachments.length) continue;
      const role = event.role === 'assistant' ? 'assistant' : 'user';
      if(role==='assistant'){const turn=createReplyTurn(box,{time:event.recorded_at}).update(text,attachments);handleHistoryVoice(event,turn);}
      else { appendChatTime(box, event.recorded_at); box.append(messageNode(role,text,{time:event.recorded_at,attachments}).node); }
    }
    if (!box.children.length) box.append(el('p', { class: 'hint', text: '这段对话还没有可显示的消息。' }));
    $('#archive-view').hidden = false;
    $('#archive-view').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    logEvent('chat', '查看归档', conversation.title || '未命名对话');
  } catch (error) {
    toast('读取档案失败：' + error.message);
    logEvent('error', '读取归档失败', error.message);
  }
}

async function restoreArchive(conversation) {
  try {
    const updated = await client.updateConversation(conversation.id, { state: 'active' });
    state.conversation = updated;
    renderCurrentConversation();
    await loadHistory();
    await loadArchive();
    toast('已恢复为当前对话；之前的当前对话已归档。');
    logEvent('system', '归档已恢复', conversation.title || '未命名对话');
  } catch (error) {
    toast('恢复失败：' + error.message);
    logEvent('error', '恢复归档失败', error.message);
  }
}

async function deleteArchive(conversation) {
  const proceed = await openModal({
    title: '永久删除这段对话？',
    hint: '会删除全部消息、附件引用及由它产生的记忆来源；不可恢复。',
    okText: '永久删除',
    danger: true,
  });
  if (!proceed) return;
  try {
    await client.deleteConversation(conversation.id);
    if (state.archiveViewId === conversation.id) {
      $('#archive-view').hidden = true;
      state.archiveViewId = null;
    }
    await loadArchive();
    toast('对话已永久删除。');
    logEvent('system', '归档对话已永久删除', conversation.title || '未命名对话', { conversation_id: conversation.id });
  } catch (error) {
    toast('删除失败：' + error.message);
    logEvent('error', '永久删除归档失败', error.message);
  }
}

/* ---------- 发送 ---------- */
const ERROR_HELP = {
  CONTEXT_TOO_LONG: {
    text: '人格档案与这段输入超出了上下文预算，这条消息没有发给模型。可以精简「灵魂档案」里过长或重复的设定，或把消息拆短。',
    action: { label: '去精简人格档案', section: 'soul' },
  },
  INPUT_TOO_LONG: {
    text: '这段输入太长了，超出了单条消息预算。请分几次发送。',
    action: { label: '查看人格档案', section: 'soul' },
  },
  MODEL_UNCONFIGURED: {
    text: '还没有可用的对话模型。请先在「模型与密钥」里配置。',
    action: { label: '去配置模型', section: 'model' },
  },
  PROVIDER_NOT_CONFIGURED: {
    text: '当前所选模型的服务商尚未配置。请检查「模型与密钥」。',
    action: { label: '去配置模型', section: 'model' },
  },
  CONVERSATION_NOT_ACTIVE: {
    text: '这段对话已归档，只能查看。请恢复它或开始新对话后再发送。',
    action: { label: '打开对话归档', section: 'archive' },
  },
};

function markFailed(message, code, partial = '', rawMessage = '') {
  message.body.hidden=false;
  message.node.classList.add('failed');
  message.node.classList.remove('pending', 'streaming');
  const help = ERROR_HELP[code];
  message.body.replaceChildren();
  if (partial) {
    message.body.textContent = partial;
    message.body.append(el('p', { class: 'hint', text: help ? help.text : (rawMessage || '生成中断：' + code) }));
  } else {
    message.body.textContent = help ? help.text : (rawMessage || '发送失败：' + code);
  }
  if (help && help.action) {
    message.bubble.append(el('button', {
      class: 'btn ghost small link-error', type: 'button', text: help.action.label,
      onclick: () => navigate(help.action.section),
    }));
  }
  if (code) message.meta.append(el('span', { class: 'err-code', text: code }));
}

async function recoverAssistantReply(conversationId, generationId) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const events = await client.history(conversationId, 200);
      const event = events.find((item) => item.kind === 'assistant_output' && item.generation_id === generationId);
      const text = event?.content?.text || '';
      const attachments = event?.attachments || event?.content?.attachments || [];
      if (text || attachments.length) return { text, attachments };
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
  }
  return null;
}

async function sendMessage(text) {
  if (!state.companionId) return;
  if (!state.conversation) {
    await loadActiveConversation();
    if (!state.conversation) return;
  }
  const images=state.pendingImages.slice();
  state.pendingImages=[];renderPendingImages();
  appendMessage('user', text,{attachments:images,animate:true});
  const assistant = appendMessage('assistant', '', { className: 'pending', animate:true });
  const turn=createLiveReplyTurn($('#messages'),{animate:true},assistant);
  const replyImages=[];
  showChatEmpty(false);
  setBusy(true);
  let failed = false;
  const startedAt = performance.now();
  let firstDeltaAt = null;
  let received = '';
  let eventCount = 0;
  let terminalReceived = false;
  let speechPayload = null;
  let segmentsSeen = false;
  let generationCompleted = false;
  logEvent('chat', '发送消息', text.slice(0, 90) + (text.length > 90 ? '…' : '') + ' · ' + text.length + ' 字', {
    conversation_id: state.conversation.id,
    characters: text.length,
    images: images.length,
  });
  try {
    const accepted = await client.chat({
      request_id: uuid(),
      conversation_id: state.conversation.id,
      input: { text,asset_ids:images.map(image=>image.asset_id) },
      output: { voice: state.voiceAlways ? 'always' : 'auto' },
      device_id: 'web-reference',
    });
    state.streaming = { generationId: accepted.generation_id, controller: new AbortController() };
    state.historyLogged.add(accepted.generation_id);
    logEvent('chat', '生成已受理', 'generation ' + String(accepted.generation_id).slice(0, 8), {
      generation_id: accepted.generation_id,
      request_id: accepted.request_id,
      events_url: accepted.events_url,
    });
    let lastEventId = null;
    let lastSequence=0;
    const handleEvent = (event) => {
      if (event.id) lastEventId = event.id;
      const payload = (event.data && event.data.payload) || {};
      const sequence=event.data?.sequence;
      if(sequence&&sequence<=lastSequence)return;
      if(sequence)lastSequence=sequence;
      eventCount += 1;
      if (event.type === 'memory.context') {
        const sources = Array.isArray(payload.source_ids) ? payload.source_ids.length : 0;
        logEvent('system', '上下文已组装', '来源 ' + sources + ' 条' + (payload.persona_version ? ' · 人格 v' + payload.persona_version : ''), {
          sources,
          memory_count: payload.memory_count,
          memory_ids: payload.memory_ids,
          token_estimate: payload.token_estimate,
          persona_version: payload.persona_version,
          model: payload.model ? payload.model.provider_id + ' · ' + payload.model.model_id : undefined,
        });
      } else if (event.type === 'text.delta' && typeof payload.text === 'string') {
        logProcessing(accepted.generation_id,event.type,{...payload,stage:'reply'});
        if (firstDeltaAt === null) {
          firstDeltaAt = performance.now();
          logEvent('chat', '开始流式返回', '首字 ' + Math.round(firstDeltaAt - startedAt) + ' ms', {
            first_token_ms: Math.round(firstDeltaAt - startedAt),
            generation_id: accepted.generation_id,
          });
        }
        received += payload.text;
      } else if (event.type === 'text.segment') {
        segmentsSeen = true;
        logProcessing(accepted.generation_id, event.type, payload);
        turn.segment(payload);
        scrollToEnd(true);
      } else if(event.type==='reasoning.delta') {
        logProcessing(accepted.generation_id,event.type,payload);
      } else if(event.type==='tool.started'||event.type==='tool.completed') {
        logProcessing(accepted.generation_id,event.type,payload);
      } else if(event.type==='sticker.sent'&&payload.attachment) {
        replyImages.push(payload.attachment);
        turn.sticker(payload.attachment);
      } else if(event.type==='speech.queued') {
        speechPayload=payload;
        logVoiceProcessing('queued',payload,accepted.generation_id);
      } else if(event.type==='text.completed') {
        /* segments are delivered one by one; nothing to flush here */
      } else if (event.type === 'warning' && payload.message) {
        toast(payload.message);
        logEvent('system', '服务提示', payload.message);
      } else if (event.type === 'generation.completed') {
        terminalReceived = true;
        generationCompleted = true;
        turn.finish('completed');
        if(turn.hasVoiceSegments())startVoiceWatchSegments(accepted.generation_id,turn);
      } else if (event.type === 'generation.failed') {
        failed = true;
        terminalReceived = true;
        turn.finish('failed');
        const failedTarget=turn.last();
        if(failedTarget)markFailed(failedTarget, payload.code || 'GENERATION_FAILED', failedTarget.partText||'');
        logEvent('error', '生成失败', payload.code || 'GENERATION_FAILED', {
          code: payload.code,
          retryable: payload.retryable,
          generation_id: accepted.generation_id,
        });
      } else if (event.type === 'generation.cancelled') {
        terminalReceived = true;
        turn.finish('cancelled');
        const cancelledTarget=turn.last();
        if(cancelledTarget)cancelledTarget.meta.append(el('span', { text: '已停止' }));
        logEvent('chat', '生成已停止', '用户中断或上游取消');
      }
    };
    try {
      let attempts = 0;
      while (true) {
        try {
          await client.stream(accepted.events_url, { signal: state.streaming.controller.signal, lastEventId, onEvent: handleEvent });
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          if (attempts >= 3) throw error;
          attempts += 1;
          logEvent('system', '事件流中断，正在续传', '第 ' + attempts + ' 次 · 已收到 ' + eventCount + ' 个事件');
          await new Promise((resolve) => setTimeout(resolve, 400 * attempts));
          continue;
        }
        if (terminalReceived) break;
        if (attempts >= 3) {
          logEvent('error', '事件流提前结束', '未收到终态事件，已收到 ' + eventCount + ' 个事件', {
            generation_id: accepted.generation_id,
            last_event_id: lastEventId,
            sse_events: eventCount,
          });
          break;
        }
        attempts += 1;
        logEvent('system', '事件流提前结束，正在续传', '第 ' + attempts + ' 次 · 已收到 ' + eventCount + ' 个事件');
        await new Promise((resolve) => setTimeout(resolve, 400 * attempts));
      }
    } finally {
      turn.settle();
    }
    if (!failed) {
      if (!received && !replyImages.length && !segmentsSeen) {
        const recovered = await recoverAssistantReply(state.conversation.id, accepted.generation_id);
        if (recovered) {
          received = recovered.text;
          replyImages.push(...recovered.attachments.filter((item) => !replyImages.some((existing) => existing.asset_id === item.asset_id)));
          logEvent('system', '已从档案恢复回复正文', '正文 ' + received.length + ' 字', {
            generation_id: accepted.generation_id,
            sse_events: eventCount,
            terminal: terminalReceived,
          });
          if (!segmentsSeen) turn.update(recovered.text, recovered.attachments);
        }
      }
      if (!received&&!replyImages.length&&!segmentsSeen) {
        const last = turn.last();
        if (last) last.body.textContent = '（本次没有收到正文，已记录诊断信息，请重试）';
        logEvent('error', '回复正文为空', 'SSE 未产生正文且档案中也没有可恢复内容', {
          generation_id: accepted.generation_id,
          sse_events: eventCount,
          terminal: terminalReceived,
          last_event_id: lastEventId,
        });
      } else if (!segmentsSeen) {
        // Older servers / replay fallback: keep the previous full-text rendering path.
        turn.update(received, replyImages);
      }
      const elapsed = performance.now() - startedAt;
      logEvent('chat', '回复完成', received.length + ' 字 · 用时 ' + (elapsed / 1000).toFixed(1) + ' 秒', {
        generation_id: accepted.generation_id,
        characters: received.length,
        elapsed_ms: Math.round(elapsed),
        sse_events: eventCount,
        terminal: terminalReceived,
        first_token_ms: firstDeltaAt === null ? undefined : Math.round(firstDeltaAt - startedAt),
      });
    }
    scrollToEnd(true);
  } catch (error) {
    turn.finish('failed');
    if (error.name === 'AbortError' || error.code === 'ABORT_ERR') {
      const last = turn.last();
      if (last) last.meta.append(el('span', { text: '已停止' }));
      logEvent('chat', '已停止生成', '本地流已断开');
    } else {
      const last = turn.last();
      if (last) markFailed(last, error.code || 'NETWORK', last.partText||'', error.message);
      logEvent('error', '发送失败', (error.code || 'NETWORK') + ' · ' + error.message, {
        code: error.code,
        status: error.status,
        retryable: error.retryable,
      });
      if (error.code !== 'STALE_CONTEXT') toast('发送失败：' + error.message);
    }
  } finally {
    state.streaming = null;
    setBusy(false);
  }
}

function isStopCommand(text) {
  return /^(?:\/\s*)?(?:stop|停止)$/i.test(text.trim());
}

function setBusy(busy) {
  state.busy=busy;
  $('#send').disabled = state.uploadingImages || (busy && !isStopCommand($('#input').value));
  $('#chat-images').disabled=busy||state.uploadingImages;
  $('#stop').hidden = !busy;
}

async function stopGeneration() {
  const streaming = state.streaming;
  stopAllAudio();
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 5000);
  try {
    let result;
    try {
      result = await client.request('/api/v1/agent/stop', { method: 'POST', signal: timeout.signal });
    } catch (error) {
      if (error.status !== 404) throw error;
      // The running service can use its existing interrupt API until its next restart.
      const pending = await client.request('/api/v1/agent/approvals', { signal: timeout.signal });
      const ids = new Set(pending.items.map(item => item.generation_id));
      if (streaming) ids.add(streaming.generationId);
      for (const id of ids) await client.request('/api/v1/generations/' + encodeURIComponent(id) + '/interrupt', { method: 'POST', body: { command_id: uuid() }, signal: timeout.signal });
      result = { stopped: ids.size };
    }
    document.dispatchEvent(new Event('agi-agent-stopped'));
    toast('已停止当前动作');
    logEvent('system', '停止命令', '已停止 ' + result.stopped + ' 个回答任务');
  } catch (error) {
    toast('本地播放已停止，服务端停止未确认：' + error.message);
  } finally {
    clearTimeout(timer);
    streaming?.controller.abort();
    setBusy(false);
  }
}

/* ---------- 图片与表情包 ---------- */
const IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
function isSupportedImage(file) {
  return IMAGE_MIME_TYPES.has(file.type) || /\.(png|jpe?g|webp|gif)$/i.test(file.name || '');
}
function rejectUnsupportedImages(files) {
  const unsupported = files.filter((file) => !isSupportedImage(file));
  if (!unsupported.length) return false;
  toast('仅支持 PNG / JPEG / WebP / GIF：' + unsupported.map((file) => file.name || '未命名文件').join('、'));
  return true;
}
function renderPendingImages() {
  const box=$('#pending-images');box.replaceChildren();
  for(const image of state.pendingImages){
    const card=el('div',{class:'pending-image'});
    const media=el('div',{class:'message-images'});card.append(media);appendImages(client,media,[image]);
    card.append(el('button',{class:'btn ghost small',type:'button',text:'移除',onclick:()=>{if(state.busy||state.uploadingImages)return;state.pendingImages=state.pendingImages.filter(item=>item!==image);renderPendingImages();}}));box.append(card);
  }
}

async function uploadChatImages(input) {
  const files=Array.from(input.files||[]);input.value='';
  await uploadChatFiles(files);
}

async function uploadChatFiles(files) {
  if(state.busy||state.uploadingImages){toast('请等待当前上传或回复完成');return;}
  if(!files.length)return;
  if(rejectUnsupportedImages(files))return;
  if(files.length+state.pendingImages.length>4){toast('最多选择 4 张图片');return;}
  state.uploadingImages=true;setBusy(state.busy);
  try{
    for(const file of files){
      const asset=await client.uploadAsset(file);
      if(!state.pendingImages.some(image=>image.asset_id===asset.id))state.pendingImages.push({asset_id:asset.id,kind:'image',name:file.name,mime:asset.mime,width:asset.metadata?.width,height:asset.metadata?.height});
      renderPendingImages();
    }
  }catch(error){toast('图片上传失败：'+error.message);}
  finally{state.uploadingImages=false;setBusy(state.busy);}
}

let stickerLoadToken = 0;
async function loadStickers() {
  if(!state.companionId)return;
  const token = ++stickerLoadToken;
  const box=$('#sticker-grid');
  if(!box)return;
  box.replaceChildren();$('#sticker-hint').textContent='读取中…';
  try{
    const rows=await client.request('/api/v1/stickers?companion_id='+encodeURIComponent(state.companionId));
    if(token!==stickerLoadToken)return; // a newer load already owns the grid
    box.replaceChildren();
    $('#sticker-hint').textContent=rows.length?'共 '+rows.length+' 张，包含已隐藏图片。':'她会在回答后决定是否把图片收进来，也可以手动上传。';
    for(const sticker of rows){
      const card=el('article',{class:'card sticker-card'});const media=el('div',{class:'message-images'});card.append(media);appendImages(client,media,[sticker.attachment]);
      const tags=(sticker.tags||[]).join(' · ')||'未打标签';
      card.append(el('strong',{text:sticker.name}),el('p',{class:'hint',text:tags}),el('p',{class:'meta',text:(sticker.source==='captured'?'收到的':'上传的')+' · 使用 '+sticker.use_count+' 次'+(sticker.last_used_at?' · '+formatTime(sticker.last_used_at):'')}));
      const actions=el('div',{class:'sticker-actions'});
      const action=(name,label,fn)=>actions.append(el('button',{class:'btn ghost small',type:'button','data-sticker-action':name,text:label,onclick:()=>Promise.resolve().then(fn).catch(error=>toast(error.message))}));
      const patch=async body=>{await client.request('/api/v1/stickers/'+sticker.id,{method:'PATCH',body});await loadStickers();};
      action('rename','改名',async()=>{const name=await openModal({title:'表情包改名',input:sticker.name});if(name)await patch({name});});
      action('tags','标签',async()=>{const tags=await openModal({title:'编辑标签',hint:'用逗号分隔，最多 20 个。',input:(sticker.tags||[]).join(',')});if(tags!==null)await patch({tags:tags.split(/[,，]/).map(t=>t.trim()).filter(Boolean)});});
      if(!(sticker.tags||[]).length)action('autotag','自动标签',async()=>{await client.request('/api/v1/stickers/'+sticker.id+'/autotag',{method:'POST'});await loadStickers();});
      action('hide',sticker.state==='hidden'?'恢复':'隐藏',()=>patch({state:sticker.state==='hidden'?'active':'hidden'}));
      action('delete','删除',async()=>{if(await openModal({title:'删除表情包',hint:'移除库中条目；聊天中已发送的图片仍保留。',danger:true,okText:'删除'})){await client.request('/api/v1/stickers/'+sticker.id,{method:'DELETE'});await loadStickers();}});
      card.append(actions);box.append(card);
    }
  }catch(error){if(token===stickerLoadToken)$('#sticker-hint').textContent='读取失败：'+error.message;}
}

async function uploadStickers(input) {
  const files=Array.from(input.files||[]);input.value='';
  if(!files.length)return;
  if(rejectUnsupportedImages(files))return;
  input.disabled=true;
  try{for(const file of files){const asset=await client.uploadAsset(file);await client.request('/api/v1/stickers',{method:'POST',body:{companion_id:state.companionId,asset_id:asset.id,name:file.name}});}await loadStickers();}
  catch(error){toast('上传失败：'+(error.message||error));}
  finally{input.disabled=false;}
}

async function uploadAvatar(input,isUser=false) {
  const file=input.files?.[0];input.value='';if(!file)return;
  input.disabled=true;$('#save-persona').disabled=true;
  try{
    const cropped=await cropAvatar(file);if(!cropped)return;
    const asset=await client.uploadAsset(cropped);
    if(isUser)state.userAvatarPending=asset.id;else state.avatarPending=asset.id;
    const prefix=isUser?'user':'persona';
    await paintAvatar($('#'+prefix+'-avatar'),isUser?(state.userName||'我'):state.personaName,asset.id);
    $('#'+prefix+'-avatar-remove').hidden=false;
    if(!isUser)updateLogAvatar();
    toast('头像已裁剪上传，点“保存档案”后生效。');
  }catch(error){toast('头像上传失败：'+error.message);}
  finally{input.disabled=false;$('#save-persona').disabled=false;}
}

/* ---------- 记忆 ---------- */
const MEMORY_KIND_LABELS = {
  preference: '偏好', fact: '事实', relationship: '关系', episode: '经历',
  commitment: '承诺', plan: '计划', identity: '身份', other: '其他',
};
const MEMORY_EPISTEMIC_LABELS = {
  user_asserted: '用户明确说过', observed: '模型观察', assistant_generated: '助手生成',
  inferred: '模型推断', fictional: '虚构',
};
const MEMORY_STATUS_LABELS = { active: '有效', hidden: '已隐藏', expired: '已过期', dormant: '休眠', superseded: '已替代', disputed: '争议', candidate: '候选' };
const MEMORY_JOB_LABELS = { pending: '排队中', running: '处理中', completed: '已完成', failed: '失败', interrupted: '已中断' };
const MEMORY_EXTRACTOR_LABELS = {
  memory_extract: '提炼使用独立模型', dialogue_fallback: '提炼使用对话模型', unconfigured: '提炼模型未配置',
};

function memoryStatusPill(status) {
  return el('span', { class: 'pill status-pill status-' + (status || 'other'), text: MEMORY_STATUS_LABELS[status] || status || '未知' });
}

function formatDateShort(value) {
  if (!value) return '无';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

function memoryMetaLine(item) {
  return el('div', { class: 'meta' }, [
    el('span', { class: 'tag', text: MEMORY_KIND_LABELS[item.kind] || item.kind || '其他' }),
    el('span', { class: 'tag', text: MEMORY_EPISTEMIC_LABELS[item.epistemic_type] || item.epistemic_type || '未知性质' }),
    memoryStatusPill(item.status),
    el('span', { text: (item.source_count || 0) + ' 个来源' }),
    el('span', { text: formatTime(item.created_at) }),
    el('span', { text: '重要度 ' + Math.round(Number(item.importance || 0.5) * 100) + '%' }),
    el('span', { text: '访问 ' + (item.access_count || 0) + ' 次' }),
    el('span', { text: '到期 ' + formatDateShort(item.expires_at) }),
  ]);
}

async function loadMemoryExtractorChip() {
  const chip = $('#memory-extractor-chip');
  try {
    const caps = await client.capabilities();
    chip.textContent = MEMORY_EXTRACTOR_LABELS[caps.memory_extraction] || '提炼状态未知';
  } catch {
    chip.textContent = '提炼状态读取失败';
  }
}

async function loadMemoryPage() {
  if (!state.companionId) return;
  $('#memory-query').value = state.memoryQuery;
  $('#memory-kind').value = state.memoryKind;
  $('#memory-status').value = state.memoryStatus;
  $('#memory-sort').value = state.memorySort;
  await Promise.all([loadMemoryExtractorChip(), loadMemoryStats(), loadMemorySettings(), loadMemoryItems(), loadMemoryJobs()]);
}

async function loadMemoryStats() {
  try {
    const stats = await client.memoryStats(state.companionId);
    state.memoryStats = stats;
    const counts = stats.counts || {};
    $('#memory-stat-active').textContent = counts.active || 0;
    $('#memory-stat-hidden').textContent = counts.hidden || 0;
    $('#memory-stat-expired').textContent = counts.expired || 0;
    $('#memory-stat-superseded').textContent = (counts.superseded || 0) + (counts.disputed || 0);
    $('#memory-stat-pending').textContent = stats.pending_events || 0;
    $('#memory-stat-jobs').textContent = stats.jobs_total || 0;
    $('#memory-distribution-hint').textContent = '共 ' + (stats.total || 0) + ' 条记忆 · 可见事件 ' + (stats.visible_events || 0) + ' 条 · 待提炼 ' + (stats.pending_events || 0) + ' 条';
    renderMemoryBars($('#memory-importance-chart'), (stats.importance_buckets || []).map((bucket) => ({ label: bucket.range, count: bucket.count })));
    const kinds = Object.entries(stats.kinds || {}).sort((a, b) => b[1] - a[1]);
    renderMemoryBars($('#memory-kind-chart'), kinds.map(([kind, count]) => ({ label: MEMORY_KIND_LABELS[kind] || kind, count })));
  } catch (error) {
    $('#memory-distribution-hint').textContent = '统计读取失败：' + error.message;
  }
}

function renderMemoryBars(container, rows) {
  if (!rows.length || rows.every((row) => !row.count)) {
    container.replaceChildren(el('p', { class: 'memory-empty', text: '暂无数据。' }));
    return;
  }
  const max = Math.max(1, ...rows.map((row) => Number(row.count) || 0));
  container.replaceChildren(...rows.map((row) => el('div', { class: 'memory-bar' }, [
    el('span', { text: row.label }),
    el('div', { class: 'memory-bar-track' }, [el('div', { class: 'memory-bar-fill', style: 'width:' + Math.max(2, (Number(row.count) || 0) / max * 100).toFixed(1) + '%' })]),
    el('span', { class: 'memory-bar-count', text: String(row.count || 0) }),
  ])));
}

async function loadMemorySettings() {
  try {
    const settings = await client.memorySettings();
    state.memorySettings = settings;
    $('#memory-extraction-enabled').checked = !!settings.extraction_enabled;
    $('#memory-batch-events').value = settings.batch_events;
    $('#memory-max-candidates').value = settings.max_candidates;
    $('#memory-timeout-seconds').value = settings.timeout_seconds;
    $('#memory-maintenance-enabled').checked = !!settings.maintenance_enabled;
    $('#memory-maintenance-hours').value = settings.maintenance_hours;
    $('#memory-embedding-chip').textContent = settings.embedding === 'keyword_only' ? '向量：未接入' : '向量：已配置未接入';
    $('#memory-settings-hint').textContent = '当前提炼：' + (MEMORY_EXTRACTOR_LABELS[settings.extractor] || settings.extractor || '未配置') + '；向量语义检索尚未接入。';
  } catch (error) {
    $('#memory-settings-hint').textContent = '设置读取失败：' + error.message;
  }
}

async function saveMemorySettings(event) {
  event.preventDefault();
  const button = $('#memory-settings-save');
  button.disabled = true;
  try {
    const settings = await client.memoryUpdateSettings({
      extraction_enabled: $('#memory-extraction-enabled').checked,
      batch_events: Number($('#memory-batch-events').value),
      max_candidates: Number($('#memory-max-candidates').value),
      timeout_seconds: Number($('#memory-timeout-seconds').value),
      maintenance_enabled: $('#memory-maintenance-enabled').checked,
      maintenance_hours: Number($('#memory-maintenance-hours').value),
    });
    state.memorySettings = settings;
    toast('记忆设置已保存并生效。');
    logEvent('system', '记忆设置已保存', '自动提炼 ' + (settings.extraction_enabled ? '开' : '关'));
    await Promise.all([loadMemorySettings(), loadMemoryExtractorChip(), loadMemoryStats()]);
  } catch (error) {
    toast('保存记忆设置失败：' + error.message);
    logEvent('error', '记忆设置保存失败', error.message);
  } finally {
    button.disabled = false;
  }
}

function openEmbeddingSettings() {
  state.modelTab = 'embedding';
  location.hash = '#/model';
}

async function loadMemoryItems() {
  const token = ++state.memoryToken;
  const list = $('#memories');
  const hint = $('#memory-hint');
  list.replaceChildren(el('li', { class: 'memory-empty', text: '正在读取记忆…' }));
  hint.textContent = '';
  try {
    const items = await client.memoryItems(state.companionId, {
      q: state.memoryQuery, kind: state.memoryKind, status: state.memoryStatus, sort: state.memorySort, limit: 100,
    });
    if (token !== state.memoryToken) return;
    state.memoryItems = items;
    $('#memory-count').textContent = items.length ? '共 ' + items.length + ' 条' : '';
    const filters = [state.memoryQuery && '“' + state.memoryQuery + '”', state.memoryKind && MEMORY_KIND_LABELS[state.memoryKind], MEMORY_STATUS_LABELS[state.memoryStatus]].filter(Boolean);
    hint.textContent = filters.length ? '筛选：' + filters.join(' · ') : '显示全部记忆条目。';
    renderMemoryList(items);
    if (!items.length) {
      state.selectedMemoryId = null;
      $('#memory-detail').hidden = true;
    }
  } catch (error) {
    if (token !== state.memoryToken) return;
    hint.textContent = '读取失败：' + error.message;
    list.replaceChildren(el('li', { class: 'memory-empty', text: '读取失败，请稍后重试。' }));
    logEvent('error', '记忆列表读取失败', error.message);
  }
}

function renderMemoryList(items) {
  const list = $('#memories');
  if (!items.length) {
    const text = state.memoryQuery
      ? '没有匹配的记忆。试试更短的关键词，或切换状态筛选。'
      : '还没有长期记忆。先聊几轮，提炼任务完成后会自动出现在这里。';
    list.replaceChildren(el('li', { class: 'memory-empty', text }));
    return;
  }
  list.replaceChildren(...items.map(renderMemoryItem));
}

function renderMemoryItem(item) {
  const node = el('li', {
    class: 'memory memory-item' + (item.id === state.selectedMemoryId ? ' selected' : ''),
    tabindex: '0',
    'data-memory-id': item.id,
    onclick: () => openMemoryDetail(item.id),
  });
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openMemoryDetail(item.id); }
  });
  node.append(memoryMetaLine(item), el('div', { class: 'content', text: item.content }));
  const actions = el('div', { class: 'memory-actions' });
  actions.append(el('button', {
    class: 'btn ghost small', type: 'button', text: '查看详情',
    onclick: (event) => { event.stopPropagation(); openMemoryDetail(item.id); },
  }));
  if (item.status === 'active') {
    actions.append(el('button', {
      class: 'btn ghost small', type: 'button', text: '纠正',
      onclick: (event) => { event.stopPropagation(); openCorrectionForItem(item); },
    }));
    actions.append(el('button', {
      class: 'btn ghost small', type: 'button', text: '隐藏',
      onclick: (event) => { event.stopPropagation(); toggleMemoryStatus(item, 'hidden'); },
    }));
  } else if (item.status === 'hidden') {
    actions.append(el('button', {
      class: 'btn ghost small', type: 'button', text: '恢复召回',
      onclick: (event) => { event.stopPropagation(); toggleMemoryStatus(item, 'active'); },
    }));
  }
  node.append(actions);
  return node;
}

function markSelectedMemoryCard() {
  for (const node of $$('#memories .memory-item')) {
    node.classList.toggle('selected', node.dataset.memoryId === state.selectedMemoryId);
  }
}

async function openMemoryDetail(memoryId) {
  try {
    const detail = await client.memoryItem(memoryId);
    state.selectedMemoryId = memoryId;
    renderMemoryDetail(detail);
    markSelectedMemoryCard();
  } catch (error) {
    toast('读取记忆详情失败：' + error.message);
    logEvent('error', '记忆详情读取失败', error.message);
  }
}

function renderMemoryDetail(memory) {
  $('#memory-detail').hidden = false;
  const body = $('#memory-detail-body');
  body.replaceChildren();
  body.append(el('p', { class: 'memory-detail-content', text: memory.content }));
  const facts = el('dl', { class: 'memory-facts' });
  const fact = (label, value) => facts.append(el('dt', { text: label }), el('dd', { text: value }));
  fact('类型', MEMORY_KIND_LABELS[memory.kind] || memory.kind || '其他');
  fact('性质', MEMORY_EPISTEMIC_LABELS[memory.epistemic_type] || memory.epistemic_type || '未知');
  fact('状态', MEMORY_STATUS_LABELS[memory.status] || memory.status || '未知');
  fact('置信度', Math.round(Number(memory.confidence || 0) * 100) + '%');
  fact('重要度', Math.round(Number(memory.importance || 0.5) * 100) + '%');
  fact('访问次数', String(memory.access_count || 0));
  fact('到期', formatDateShort(memory.expires_at));
  fact('衰减', memory.decay_type || 'exponential');
  fact('上次访问', memory.last_accessed_at ? formatTime(memory.last_accessed_at) : '从未');
  fact('创建时间', formatTime(memory.created_at));
  fact('记忆 ID', memory.id);
  body.append(facts);

  body.append(el('p', { class: 'meta', text: '来源证据' }));
  const sources = el('div', { class: 'memory-sources' });
  const refs = memory.source_refs || [];
  if (!refs.length) sources.append(el('p', { class: 'meta', text: '没有可显示的来源。' }));
  for (const ref of refs) {
    const box = el('div', { class: 'memory-source' });
    box.append(el('p', { class: 'quote', text: ref.available === false ? '来源已删除或不可访问' : (ref.quote || '（没有引文）') }));
    box.append(el('p', { class: 'meta', text: '事件 ' + ref.event_id + (ref.occurred_at ? ' · ' + formatTime(ref.occurred_at) : '') }));
    sources.append(box);
  }
  body.append(sources);

  const actions = el('div', { class: 'memory-detail-actions' });
  if (memory.status === 'active') {
    actions.append(el('button', { class: 'btn ghost small', type: 'button', text: '纠正', onclick: () => openCorrection(memory) }));
    if (Number(memory.importance || 0) < 0.85) {
      actions.append(el('button', { class: 'btn ghost small', type: 'button', text: '标为重要', onclick: () => setMemoryImportance(memory, 0.9) }));
    }
    actions.append(el('button', { class: 'btn ghost small', type: 'button', text: '隐藏（停止召回）', onclick: () => toggleMemoryStatus(memory, 'hidden') }));
  } else if (memory.status === 'hidden') {
    actions.append(el('button', { class: 'btn primary small', type: 'button', text: '恢复召回', onclick: () => toggleMemoryStatus(memory, 'active') }));
  }
  body.append(actions);
}

async function openCorrectionForItem(item) {
  try {
    const detail = await client.memoryItem(item.id);
    openCorrection(detail);
  } catch (error) {
    toast('读取来源失败：' + error.message);
  }
}

function openCorrection(memory) {
  const refs = (memory.source_refs || []).filter((ref) => ref.available !== false && ref.event_id);
  if (!refs.length) { toast('这条记忆没有可用来源，暂时不能纠正。'); return; }
  state.pendingMemory = memory;
  $('#correction-target').textContent = '原记忆：' + memory.content;
  const select = $('#correction-source');
  select.replaceChildren(...refs.map((ref) => el('option', {
    value: ref.event_id,
    text: (ref.quote || ref.event_id).slice(0, 90),
  })));
  const updateQuote = () => {
    const ref = refs.find((item) => item.event_id === select.value) || refs[0];
    $('#correction-quote').textContent = ref && ref.quote ? '引文：' + ref.quote : '';
  };
  select.onchange = updateQuote;
  updateQuote();
  $('#correction-text').value = '';
  $('#memory-edit').hidden = false;
  setTimeout(() => $('#correction-text').focus(), 0);
}

function closeCorrection() {
  $('#memory-edit').hidden = true;
  state.pendingMemory = null;
}

async function applyCorrection() {
  const memory = state.pendingMemory;
  if (!memory) return;
  const text = $('#correction-text').value.trim();
  const source = $('#correction-source').value;
  if (!text) { toast('请填写更准确的说法。'); $('#correction-text').focus(); return; }
  if (!source) { toast('这条记忆没有可绑定的来源事件。'); return; }
  try {
    await client.correction(memory.id, text, source);
    closeCorrection();
    toast('纠正已写入：旧记忆转为历史，新记忆带来源引用。');
    logEvent('system', '记忆已纠正', text.slice(0, 80));
    await Promise.all([loadMemoryItems(), loadMemoryJobs()]);
    if (state.selectedMemoryId && state.selectedMemoryId === memory.id) {
      const list = await client.memoryItems(state.companionId, { status: 'active', limit: 100 });
      const replacement = list.find((item) => item.content === text);
      if (replacement) await openMemoryDetail(replacement.id);
    }
  } catch (error) {
    toast('纠正失败：' + error.message);
    logEvent('error', '记忆纠正失败', error.message);
  }
}

async function toggleMemoryStatus(item, status) {
  try {
    const result = await client.memorySetStatus(item.id, status);
    if (result && result.memory_revision) state.memoryRevision = result.memory_revision;
    toast(status === 'hidden' ? '已隐藏：不再进入召回，原始对话仍保留。' : '已恢复：重新参与召回。');
    logEvent('system', status === 'hidden' ? '记忆已隐藏' : '记忆已恢复', item.content.slice(0, 80));
    await Promise.all([loadMemoryItems(), loadMemoryJobs()]);
    if (status === 'hidden' && state.selectedMemoryId === item.id) {
      state.selectedMemoryId = null;
      $('#memory-detail').hidden = true;
    }
    if (status === 'active' && state.selectedMemoryId === item.id) await openMemoryDetail(item.id);
  } catch (error) {
    toast('修改记忆状态失败：' + error.message);
    logEvent('error', '记忆状态修改失败', error.message);
  }
}

async function setMemoryImportance(memory, importance) {
  try {
    await client.memorySetImportance(memory.id, importance);
    toast('重要度已提升，生命周期会相应延长。');
    logEvent('system', '记忆重要度调整', memory.content.slice(0, 80));
    await loadMemoryItems();
    if (state.selectedMemoryId === memory.id) await openMemoryDetail(memory.id);
  } catch (error) {
    toast('调整重要度失败：' + error.message);
    logEvent('error', '记忆重要度调整失败', error.message);
  }
}

async function runRecallTest(event) {
  event.preventDefault();
  const query = $('#memory-recall-query').value.trim();
  const box = $('#memory-recall-results');
  if (!query) { toast('请输入要测试的句子。'); return; }
  box.replaceChildren(el('p', { class: 'memory-empty', text: '正在召回…' }));
  try {
    const hits = await client.memoryRecallTest(state.companionId, query);
    if (!hits.length) {
      box.replaceChildren(el('p', { class: 'memory-empty', text: '没有召回结果。试试更短的关键词。' }));
      return;
    }
    box.replaceChildren(...hits.map((hit) => {
      const routes = (hit.routes || []).map((route) => route === 'structured' ? '结构化' : route === 'keyword' ? '关键词' : route).join(' + ');
      const meta = el('div', { class: 'meta' }, [
        el('span', { class: 'tag', text: routes || '未知路线' }),
        el('span', { text: '分数 ' + Number(hit.score || 0).toFixed(3) }),
      ]);
      if (hit.kind) meta.append(el('span', { text: MEMORY_KIND_LABELS[hit.kind] || hit.kind }));
      if (hit.importance != null) meta.append(el('span', { text: '重要度 ' + Math.round(Number(hit.importance) * 100) + '%' }));
      if (hit.freshness != null) meta.append(el('span', { text: '新鲜度 ' + Math.round(Number(hit.freshness) * 100) + '%' }));
      if (hit.status) meta.append(memoryStatusPill(hit.status));
      const row = el('div', { class: 'recall-hit' }, [meta, el('p', { class: 'recall-content', text: hit.content || '' })]);
      if (hit.memory_id) {
        row.append(el('button', { class: 'btn ghost small', type: 'button', text: '查看记忆', onclick: () => openMemoryDetail(hit.memory_id) }));
      }
      return row;
    }));
    logEvent('chat', '记忆召回测试', '“' + query.slice(0, 60) + '” · ' + hits.length + ' 条');
  } catch (error) {
    box.replaceChildren(el('p', { class: 'memory-empty', text: '召回失败：' + error.message }));
    logEvent('error', '记忆召回测试失败', error.message);
  }
}

async function maintainMemories() {
  const button = $('#memory-maintain');
  button.disabled = true;
  try {
    const result = await client.memoryMaintenance(state.companionId);
    toast('生命周期维护完成：过期 ' + (result.expired || 0) + ' 条。');
    logEvent('system', '记忆生命周期维护', '过期 ' + (result.expired || 0) + ' 条');
    await Promise.all([loadMemoryItems(), loadMemoryJobs()]);
  } catch (error) {
    toast('维护失败：' + error.message);
    logEvent('error', '记忆生命周期维护失败', error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadMemoryJobs() {
  const box = $('#memory-jobs');
  box.replaceChildren(el('p', { class: 'memory-empty', text: '正在读取提炼任务…' }));
  try {
    const jobs = await client.memoryJobs(state.companionId);
    state.memoryJobs = jobs;
    if (!jobs.length) {
      box.replaceChildren(el('p', { class: 'memory-empty', text: '暂无提炼任务。完成一轮聊天后会自动排队，也可以点“立即提炼一批”。' }));
      return;
    }
    box.replaceChildren(...jobs.map((job) => {
      const row = el('div', { class: 'memory-job' });
      row.append(el('span', { class: 'state ' + job.state, text: MEMORY_JOB_LABELS[job.state] || job.state }));
      row.append(el('span', { class: 'meta', text: formatTime(job.created_at) }));
      row.append(el('span', { class: 'meta', text: '尝试 ' + job.attempts + ' 次' }));
      const result = job.result || {};
      if (result.events !== undefined) {
        row.append(el('span', { class: 'meta', text: '事件 ' + result.events + ' · 写入 ' + (result.stored || 0) + ' · 忽略 ' + (result.ignored || 0) + ' · 隔离 ' + (result.quarantined || 0) }));
      }
      if (job.last_error_code) row.append(el('span', { class: 'meta', text: '错误 ' + job.last_error_code }));
      return row;
    }));
  } catch (error) {
    box.replaceChildren(el('p', { class: 'memory-empty', text: '读取提炼任务失败：' + error.message }));
  }
}

async function extractMemoryNow() {
  const button = $('#memory-extract-now');
  button.disabled = true;
  try {
    await client.memoryExtractNow(state.companionId);
    toast('已排队一批提炼任务，完成后会自动刷新。');
    logEvent('system', '记忆提炼', '手动排队一批历史事件');
    for (const delay of [1500, 4000, 8000]) {
      setTimeout(() => { loadMemoryItems(); loadMemoryJobs(); }, delay);
    }
  } catch (error) {
    toast('排队失败：' + error.message);
    logEvent('error', '记忆提炼排队失败', error.message);
  } finally {
    button.disabled = false;
  }
}

/* ---------- 模型与密钥 ---------- */
function currentModelTab() {
  return MODEL_TABS.find((tab) => tab.key === state.modelTab) || MODEL_TABS[0];
}

async function loadModelChip() {
  const chip = $('#model-chip');
  if (!chip) return;
  try {
    const settings = await client.request('/api/v1/settings/providers');
    state.settings = settings;
    const dialogue = (settings.models || {}).dialogue;
    chip.textContent = dialogue ? dialogue.provider_id + ' · ' + dialogue.model_id : '模型未配置';
  } catch {
    chip.textContent = '模型未配置';
  }
}

async function loadTtsData() {
  await loadVoiceSettings();
  try {
    const result = await client.voices();
    state.voiceVoices = Array.isArray(result) ? result : (result?.voices || []);
  } catch {
    state.voiceVoices = [];
  }
}

function renderTtsSettings() {
  const settings = state.voiceSettings || {};
  const voices = state.voiceVoices || [];
  const enabled = $('#tts-enabled');
  if (enabled) enabled.checked = Boolean(settings.enabled);
  const baseUrl = $('#tts-base-url');
  if (baseUrl) baseUrl.value = settings.base_url || 'http://127.0.0.1:8137';
  const probability = state.voiceProbability;
  const range = $('#tts-probability');
  if (range) range.value = String(probability);
  const rangeValue = $('#tts-probability-value');
  if (rangeValue) rangeValue.textContent = probability + '%';
  const breakProbability = Math.round(Number(settings.text_break_probability ?? 0.35) * 100);
  const breakRange = $('#tts-break-probability');
  if (breakRange) breakRange.value = String(breakProbability);
  const breakValue = $('#tts-break-probability-value');
  if (breakValue) breakValue.textContent = breakProbability + '%';
  const maxChars = $('#tts-max-chars');
  if (maxChars) maxChars.value = String(settings.max_chars || 400);
  const voice = $('#tts-voice');
  if (voice) {
    const keep = settings.voice_id || voice.value || '';
    voice.replaceChildren(el('option', { value: '', text: '自动 / 默认' }));
    for (const item of voices) {
      voice.append(el('option', { value: item.id, text: item.id + (item.name && item.name !== item.id ? ' · ' + item.name : '') + (item.saved === false ? ' · 临时' : '') }));
    }
    voice.value = voices.some((item) => item.id === keep) ? keep : '';
  }
  const availability = $('#tts-availability');
  if (availability) {
    availability.textContent = settings.available ? ('可用' + (settings.sample_rate ? ' · ' + settings.sample_rate + 'Hz' : '')) : '本地服务未连接';
  }
  const status = $('#tts-status');
  if (status) status.textContent = settings.available ? '' : '启动本地 Breeze 服务后刷新；设置可先保存。';
}

function ttsFormValues() {
  return {
    enabled: Boolean($('#tts-enabled')?.checked),
    base_url: ($('#tts-base-url')?.value || '').trim(),
    voice_id: $('#tts-voice')?.value || '',
    probability: probabilityPayload(Number($('#tts-probability')?.value || state.voiceProbability || 0)),
    text_break_probability: probabilityPayload(Number($('#tts-break-probability')?.value || 35)),
    max_chars: Number($('#tts-max-chars')?.value || 400),
  };
}

async function saveTtsSettings() {
  const body = ttsFormValues();
  if (!body.voice_id) delete body.voice_id;
  if (!body.base_url) delete body.base_url;
  body.max_chars = Math.max(20, Math.min(1000, Number(body.max_chars) || 400));
  const button = $('#tts-save');
  if (button) button.disabled = true;
  try {
    const settings = await client.voiceUpdateSettings(body);
    state.voiceSettings = settings;
    state.voiceProbability = probabilityPercent(settings.probability);
    updateVoiceControls();
    renderModelTabs();
    renderTtsSettings();
    toast('语音设置已保存。');
    logEvent('system', '语音设置已保存', '默认音色 ' + (settings.voice_id || '自动') + ' · 概率 ' + state.voiceProbability + '%');
  } catch (error) {
    toast('保存语音设置失败：' + error.message);
    logEvent('error', '保存语音设置失败', error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadProactiveSettings() {
  const form = $('#proactive-settings-form');
  if (!form) return;
  if (!state.companionId) { form.hidden = true; return; }
  form.hidden = false;
  try {
    const data = await client.request('/api/v1/proactive/settings?companion_id=' + encodeURIComponent(state.companionId));
    $('#proactive-enabled').checked = Boolean(data.enabled);
    $('#proactive-idle').value = data.min_idle_minutes;
    $('#proactive-gap').value = data.min_gap_minutes;
    $('#proactive-max').value = data.max_per_day;
    $('#proactive-quiet-start').value = data.quiet_start;
    $('#proactive-quiet-end').value = data.quiet_end;
    $('#proactive-state').textContent = data.enabled ? '已开启' : '已关闭';
    const parts = [];
    if (Number.isFinite(Number(data.sent_today))) parts.push('今天已发 ' + data.sent_today + ' 条');
    if (data.last_sent_at) {
      try { parts.push('最近 ' + new Date(data.last_sent_at).toLocaleString()); } catch {}
    }
    if (data.reason && data.reason !== 'ok' && data.enabled) parts.push('当前不会发送：' + proactiveReasonLabel(data.reason));
    $('#proactive-meta').textContent = parts.join(' · ');
    $('#proactive-hint').textContent = '';
  } catch (error) {
    $('#proactive-hint').textContent = '读取失败：' + error.message;
  }
}

function proactiveReasonLabel(reason) {
  return ({
    disabled: '功能默认关闭',
    no_active_conversation: '没有进行中的对话',
    generation_running: '正在生成回复',
    not_idle: '用户还在活跃',
    gap: '距上一条太近',
    daily_cap: '已达到今日上限',
    quiet_hours: '处于安静时段',
    model_unconfigured: '对话模型未配置',
  })[reason] || reason;
}

async function saveProactiveSettings(event) {
  event.preventDefault();
  if (!state.companionId) return;
  const hint = $('#proactive-hint');
  const button = event.target.querySelector('button[type=submit]');
  if (button) button.disabled = true;
  hint.textContent = '正在保存…';
  try {
    const data = await client.request('/api/v1/proactive/settings', {
      method: 'PATCH',
      body: {
        enabled: $('#proactive-enabled').checked,
        min_idle_minutes: Number($('#proactive-idle').value),
        min_gap_minutes: Number($('#proactive-gap').value),
        max_per_day: Number($('#proactive-max').value),
        quiet_start: $('#proactive-quiet-start').value,
        quiet_end: $('#proactive-quiet-end').value,
      },
    });
    $('#proactive-state').textContent = data.enabled ? '已开启' : '已关闭';
    hint.textContent = '已保存';
    logEvent('system', '主动消息设置已保存', data.enabled ? '已开启' : '已关闭');
    await loadProactiveSettings();
  } catch (error) {
    hint.textContent = '保存失败：' + error.message;
  } finally {
    if (button) button.disabled = false;
  }
}

async function testProactiveMessage() {
  if (!state.companionId) return;
  const hint = $('#proactive-hint');
  const button = $('#proactive-test');
  if (button) button.disabled = true;
  hint.textContent = '正在生成…';
  try {
    await client.request('/api/v1/proactive/send', { method: 'POST', body: { companion_id: state.companionId } });
    hint.textContent = '已发送';
    await loadProactiveSettings();
  } catch (error) {
    hint.textContent = '发送失败：' + error.message;
  } finally {
    if (button) button.disabled = false;
  }
}


async function previewTts() {
  const text = ($('#tts-test-text')?.value || '').trim();
  const emotion = $('#tts-emotion')?.value || '';
  if (!text) { toast('请先填写试听文本。'); return; }
  const button = $('#tts-test');
  const player = $('#tts-preview-player');
  if (button) { button.disabled = true; button.textContent = '生成中…'; }
  try {
    const blob = await client.voicePreview({ text, emotion });
    if (state.ttsPreviewUrl) { try { URL.revokeObjectURL(state.ttsPreviewUrl); } catch {} }
    state.ttsPreviewUrl = URL.createObjectURL(blob);
    if (player) {
      player.src = state.ttsPreviewUrl;
      player.hidden = false;
      player.play().catch(() => {});
    }
    $('#tts-status').textContent = '试听已生成';
  } catch (error) {
    $('#tts-status').textContent = '试听失败：' + error.message;
    toast('语音试听失败：' + error.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = '试听'; }
  }
}

async function loadModelPage() {
  await loadModelChip();
  await loadProactiveSettings().catch(() => {});
  if (state.modelTab === 'tts') {
    await loadTtsData();
    const providerLayout = $('.provider-layout');
    if (providerLayout) providerLayout.hidden = true;
    $('#tts-settings').hidden = false;
    renderModelTabs();
    renderTtsSettings();
    return;
  }
  const providerLayout = $('.provider-layout');
  if (providerLayout) providerLayout.hidden = false;
  $('#tts-settings').hidden = true;
  try {
    state.settings = await client.request('/api/v1/settings/providers');
  } catch (error) {
    $('#provider-list').replaceChildren(el('p', { class: 'hint', text: '读取服务商配置失败：' + error.message }));
    logEvent('error', '读取模型配置失败', error.message);
    return;
  }
  renderModelTabs();
  renderModelProviders();
  renderProviderDetail();
}

function providerRows() {
  const tab = currentModelTab();
  const providers = (state.settings && state.settings.providers) || [];
  const rows = providers.filter((provider) => {
    const capabilities = provider.capabilities || [];
    return capabilities.includes(tab.capability) || (tab.key === 'dialogue' && capabilities.includes('text'));
  });
  if (tab.key === 'embedding' && !rows.some((provider) => provider.id === 'local')) {
    rows.push({ id: 'local', builtin: true, capabilities: ['embedding'], models: {} });
  }
  return rows;
}

function renderModelTabs() {
  const box = $('#model-tabs');
  box.replaceChildren();
  for (const tab of MODEL_TABS) {
    const active = tab.key === state.modelTab;
    const ready = tab.key === 'tts' ? Boolean(state.voiceSettings?.available) : tab.ready;
    box.append(el('button', {
      class: 'model-tab' + (active ? ' active' : ''), type: 'button', role: 'tab', 'aria-selected': String(active),
      onclick: () => { state.modelTab = tab.key; state.selectedProviderId = null; loadModelPage(); },
    }, [
      el('span', { text: tab.label }),
      el('span', { class: 'tab-badge ' + (ready ? 'ok' : 'wait'), text: ready ? '已接入' : '待接入' }),
      el('span', { class: 'mode-indicator', 'aria-hidden': 'true' }),
    ]));
  }
}

function renderModelProviders() {
  const list = $('#provider-list');
  list.replaceChildren();
  const rows = providerRows();
  const tab = currentModelTab();
  if (state.selectedProviderId !== '__new__' && !rows.some((provider) => provider.id === state.selectedProviderId)) {
    state.selectedProviderId = rows.length ? rows[0].id : null;
  }
  for (const provider of rows) {
    const model = (provider.models || {})[tab.role];
    const active = provider.id === state.selectedProviderId;
    list.append(el('li', { class: 'provider-row' + (active ? ' active' : ''), onclick: () => { state.selectedProviderId = provider.id; renderModelProviders(); renderProviderDetail(); } }, [
      el('div', { class: 'provider-row-main' }, [
        el('strong', { text: provider.builtin ? '本地检索' : provider.id }),
        el('span', { class: 'hint', text: provider.builtin ? '内置关键词检索' : (model ? model.model_id : '尚未选择模型') }),
      ]),
      el('span', { class: 'tag', text: provider.builtin ? '内置' : (provider.credential_configured ? '密钥已保存' : '未保存密钥') }),
    ]));
  }
  $('#provider-list-hint').textContent = rows.length ? '' : '这个能力还没有提供商源，点“＋ 新增”添加。';
}

function mergedCapabilities(provider, tab) {
  const caps = new Set((provider && provider.capabilities) || (tab.key === 'dialogue' ? ['text', 'streaming'] : []));
  caps.add(tab.capability);
  if (tab.key === 'dialogue') { caps.add('text'); caps.add('streaming'); }
  return [...caps];
}

function renderProviderDetail() {
  const box = $('#provider-detail');
  box.replaceChildren();
  const tab = currentModelTab();
  const rows = providerRows();
  const selected = rows.find((provider) => provider.id === state.selectedProviderId);
  if (state.selectedProviderId === '__new__' || !selected) {
    if (!rows.length || state.selectedProviderId === '__new__') {
      box.append(providerForm(null, tab));
      return;
    }
    box.append(el('p', { class: 'hint', text: '请选择一个提供商源。' }));
    return;
  }
  if (selected.builtin) {
    box.append(
      el('h3', { text: '本地检索（内置）' }),
      el('p', { class: 'hint', text: '中文关键词 + 精确子串检索，不需要 API Key，也不会生成向量。' }),
      el('div', { class: 'row end' }, [
        el('button', {
          class: 'btn primary small', type: 'button', text: '使用本地检索',
          onclick: async () => {
            try {
              await client.request('/api/v1/settings/models/embedding', { method: 'PUT', body: { provider_id: 'local', model_id: 'keyword' } });
              toast('嵌入角色已指向本地关键词检索。');
              await loadModelPage();
            } catch (error) { toast('保存失败：' + error.message); }
          },
        }),
      ]),
    );
    return;
  }
  box.append(providerForm(selected, tab));
}

function providerForm(provider, tab) {
  const isNew = provider === null;
  const idInput = el('input', { class: 'input', placeholder: '提供商源 ID，例如 deepseek', value: isNew ? '' : provider.id });
  const baseInput = el('input', { class: 'input', placeholder: 'https://api.deepseek.com', value: isNew ? '' : (provider.base_url || '') });
  const modelRef = provider && provider.models ? provider.models[tab.role] : null;
  const modelInput = el('input', { class: 'input', placeholder: tab.key === 'dialogue' ? '例如 deepseek-chat' : '该能力对应的模型 ID', value: modelRef ? modelRef.model_id : '' });
  const keyInput = el('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: provider && provider.credential_configured ? '已保存，留空不修改' : 'API Key' });
  const status = el('p', { class: 'hint' });
  const head = el('div', { class: 'provider-detail-head' }, [
    el('h3', { text: isNew ? '新增提供商源' : provider.id }),
    el('span', { class: 'tag ' + (tab.ready ? 'ok-tag' : 'wait-tag'), text: tab.ready ? '已接入' : '待接入' }),
  ]);
  const note = el('p', { class: 'hint', text: tab.hint });
  const fields = el('div', { class: 'fields' }, [
    el('label', { class: 'field' }, [el('span', { text: '提供商源 ID' }), idInput]),
    el('label', { class: 'field' }, [el('span', { text: 'Base URL' }), baseInput]),
    el('label', { class: 'field' }, [el('span', { text: '模型 ID' }), modelInput]),
    el('label', { class: 'field' }, [el('span', { text: 'API Key' }), keyInput]),
  ]);
  const actions = [];
  actions.push(el('button', {
    class: 'btn primary small', type: 'button', text: isNew ? '添加' : '保存',
    onclick: async () => {
      const id = isNew ? idInput.value.trim() : provider.id;
      if (!id) { status.textContent = '需要提供商源 ID。'; return; }
      if (!/^[A-Za-z0-9_-]+$/.test(id)) { status.textContent = 'ID 只能包含字母、数字、下划线与连字符。'; return; }
      if (!baseInput.value.trim()) { status.textContent = '需要 Base URL。'; return; }
      status.textContent = '保存中…';
      try {
        await client.request('/api/v1/settings/providers/' + encodeURIComponent(id), {
          method: 'PUT',
          body: {
            base_url: baseInput.value.trim(),
            model_id: modelInput.value.trim() || undefined,
            api_key: keyInput.value.trim() || undefined,
            role: tab.role,
            capabilities: mergedCapabilities(provider, tab),
          },
        });
        state.selectedProviderId = id;
        status.textContent = '已保存；下一轮对话生效。';
        await loadModelPage();
        await loadModelChip();
        await refreshService();
        toast('提供商源已保存。');
        logEvent('system', '提供商源已保存', id + ' · ' + tab.role + (modelInput.value.trim() ? ' · ' + modelInput.value.trim() : ''));
      } catch (error) {
        status.textContent = '保存失败：' + error.message;
        logEvent('error', '提供商源保存失败', id + ' · ' + error.message);
      }
    },
  }));
  if (!isNew) {
    actions.push(el('button', {
      class: 'btn ghost small', type: 'button', text: '测试连接',
      onclick: async () => {
        status.textContent = '正在用服务端保存的密钥探测…';
        try {
          const result = await client.request('/api/v1/settings/providers/' + encodeURIComponent(provider.id) + '/test', { method: 'POST' });
          status.textContent = (result.ok ? '连接正常：' : '连接失败：') + result.message;
          logEvent(result.ok ? 'system' : 'error', result.ok ? '模型连接测试通过' : '模型连接测试失败', provider.id + ' · ' + result.message);
        } catch (error) {
          status.textContent = '测试失败：' + error.message;
          logEvent('error', '模型连接测试失败', provider.id + ' · ' + error.message);
        }
      },
    }));
    actions.push(el('button', {
      class: 'btn ghost small danger-text', type: 'button', text: '删除',
      onclick: async () => {
        const proceed = await openModal({
          title: '删除提供商源 ' + provider.id + '？',
          hint: '会同时删除本地保存的 API Key，并解除所有引用它的模型角色。聊天记录不受影响。',
          okText: '删除', danger: true,
        });
        if (!proceed) return;
        try {
          await client.request('/api/v1/settings/providers/' + encodeURIComponent(provider.id), { method: 'DELETE' });
          state.selectedProviderId = null;
          toast('提供商源已删除。');
          await loadModelPage();
          await refreshService();
          logEvent('system', '提供商源已删除', provider.id);
        } catch (error) {
          toast('删除失败：' + error.message);
          logEvent('error', '删除提供商源失败', provider.id + ' · ' + error.message);
        }
      },
    }));
  }
  return el('div', { class: 'provider-form' }, [
    head,
    note,
    fields,
    el('div', { class: 'row end' }, actions),
    status,
  ]);
}

/* ---------- 绑定 ---------- */
function bind() {
  updateVoiceControls();
  $('#voice-toggle').addEventListener('click', () => setVoiceAlways(!state.voiceAlways));
  $('#tts-save').addEventListener('click', saveTtsSettings);
  $('#tts-test').addEventListener('click', previewTts);
  $('#tts-refresh').addEventListener('click', async () => {
    await loadTtsData();
    renderTtsSettings();
    renderModelTabs();
  });
  $('#tts-probability').addEventListener('input', (event) => {
    const value = $('#tts-probability-value');
    if (value) value.textContent = Number(event.target.value) + '%';
  });
  $('#tts-break-probability').addEventListener('input', (event) => {
    const value = $('#tts-break-probability-value');
    if (value) value.textContent = Number(event.target.value) + '%';
  });
  $('#gate-connect').addEventListener('click', async () => {
    const button = $('#gate-connect');
    button.disabled = true;
    button.textContent = '正在连接…';
    $('#gate-hint').textContent = '正在连接服务器…';
    try {
      await connectToServer($('#gate-server').value, $('#gate-key').value);
      $('#gate-hint').textContent = '';
    } catch (error) {
      $('#gate-hint').textContent = '连接失败：' + error.message;
    } finally {
      button.disabled = false;
      button.textContent = '连接';
    }
  });
  for (const id of ['#gate-server', '#gate-key']) {
    $(id).addEventListener('keydown', (event) => { if (event.key === 'Enter') $('#gate-connect').click(); });
  }
  $('#connect').addEventListener('click', async () => {
    const button = $('#connect');
    button.disabled = true;
    $('#connection-state').textContent = '正在连接…';
    try {
      const target = await connectToServer($('#server-address').value, $('#server-key').value);
      $('#connection-state').textContent = '已连接：' + target;
      toast('已连接服务器。');
      logEvent('system', '服务器已连接', target);
    } catch (error) {
      $('#connection-state').textContent = '连接失败：' + error.message;
      logEvent('error', '连接服务器失败', error.message);
    } finally {
      button.disabled = false;
    }
  });
  $('#disconnect').addEventListener('click', async () => {
    try { localStorage.removeItem(SERVER_STORAGE); } catch {}
    client.setBase('');
    client.clearBearer();
    await client.logout().catch(() => {});
    location.reload();
  });
  $('#sign-out').addEventListener('click', async () => {
    if (state.serverMode) {
      try { localStorage.removeItem(SERVER_STORAGE); } catch {}
      location.reload();
      return;
    }
    await client.logout().catch(() => {});
    location.reload();
  });
  $('#theme-toggle').addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
  $$('[data-theme-choice]').forEach((card) => {
    card.addEventListener('click', () => applyTheme(card.dataset.themeChoice));
  });
  $('#refresh-page').addEventListener('click', async () => {
    try {
      if (state.currentPage === 'chat') await loadChatPage(true);
      else await PAGES[state.currentPage].load();
      toast('已刷新当前页面。');
    } catch (error) {
      toast('刷新失败：' + error.message);
    }
  });
  $$('.nav-item[data-page]').forEach((item) => {
    item.addEventListener('click', () => navigate(item.dataset.page));
  });
  window.addEventListener('hashchange', () => renderPage(pageFromHash()));
  window.addEventListener('resize', syncLogScrim);
  const modelChip = $('#model-chip');
  if (modelChip) modelChip.addEventListener('click', () => navigate('model'));
  $('#log-toggle').addEventListener('click', toggleLog);
  $('#log-close').addEventListener('click', closeLog);
  $('#log-scrim').addEventListener('click', closeLog);
  $('#log-filters').addEventListener('click', (event) => {
    const button = event.target.closest('.log-filter');
    if (!button) return;
    state.termFilter = button.dataset.filter;
    $$('.log-filter').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-selected', String(active));
    });
    renderTerm();
  });
  $('#term-follow').addEventListener('click', () => {
    state.termFollow = !state.termFollow;
    updateTermControls({ scroll: state.termFollow });
  });
  $('#term-detail').addEventListener('click', () => {
    state.termDetail = !state.termDetail;
    renderTerm();
  });
  $('#term-copy').addEventListener('click', copyTermLogs);
  $('#log-search').addEventListener('input', (event) => {
    state.termSearch = event.target.value;
    renderTerm();
  });
  $('#term-jump').addEventListener('click', () => {
    state.termFollow = true;
    updateTermControls({ scroll: true });
  });
  $('#log-scroll').addEventListener('scroll', () => {
    const scroll = $('#log-scroll');
    const near = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
    if (!near && state.termFollow) { state.termFollow = false; updateTermControls(); }
    else if (near && !state.termFollow) { state.termFollow = true; updateTermControls(); }
  });
  $('#log-clear').addEventListener('click', () => {
    state.termLines = [];
    state.logEntries = [];
    state.logUnread = 0;
    persistLogEntries();
    updateLogBadge();
    renderTerm();
    toast('日志视图已清空，新日志会继续追加。');
  });

  $('#conversation-menu-button').addEventListener('click', (event) => { event.stopPropagation(); toggleSessionMenu(); });
  $('#session-menu').addEventListener('click', (event) => {
    const action = event.target.closest('button') && event.target.closest('button').dataset.action;
    if (action === 'rename') renameCurrentConversation();
    if (action === 'archive') archiveCurrentConversation();
    if (action === 'delete') deleteCurrentConversation();
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu-wrap')) closeSessionMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    closeSessionMenu();
    if (state.logOpen) closeLog();
    if (!$('#memory-edit').hidden) closeCorrection();
    if (!$('#modal').hidden) finishModal(null);
  });
  $('#archive-view-close').addEventListener('click', () => { $('#archive-view').hidden = true; state.archiveViewId = null; });
  $('#provider-add').addEventListener('click', () => { state.selectedProviderId = '__new__'; renderModelProviders(); renderProviderDetail(); });

  $('#composer').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = $('#input');
    const text = input.value.trim();
    if (isStopCommand(text)) {
      input.value = ''; input.style.height = '';
      await stopGeneration();
      return;
    }
    if (state.busy||state.uploadingImages||(!text&&!state.pendingImages.length)) return;
    input.value = '';
    input.style.height = '';
    await sendMessage(text);
  });
  $('#input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); }
  });
  $('#input').addEventListener('input', (event) => {
    const node = event.target;
    node.style.height = 'auto';
    node.style.height = Math.min(node.scrollHeight, 180) + 'px';
    $('#send').disabled = state.uploadingImages || (state.busy && !isStopCommand(node.value));
  });
  $('#stop').addEventListener('click', stopGeneration);
  $('#chat-images').addEventListener('change',event=>uploadChatImages(event.target));
  $('#input').addEventListener('paste',event=>{
    const files=Array.from(event.clipboardData?.items||[]).filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
    if(!files.length)return;
    event.preventDefault();uploadChatFiles(files);
  });
  $('#sticker-upload').addEventListener('change',event=>uploadStickers(event.target));
  $('#save-persona').addEventListener('click', savePersona);
  $('#toggle-raw').addEventListener('click', () => { const box = $('#persona-raw'); box.hidden = !box.hidden; });
  $('#persona-size-raw').addEventListener('click', () => { $('#persona-raw').hidden = false; $('#persona-raw').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); });
  $('#persona-migrate-button').addEventListener('click', normalizePersona);
  $('#persona-avatar-file').addEventListener('change',event=>uploadAvatar(event.target));
  $('#user-avatar-file').addEventListener('change',event=>uploadAvatar(event.target,true));
  $('#user-avatar-remove').addEventListener('click',()=>{
    state.userAvatarPending=null;paintAvatar($('#user-avatar'),state.userName||'我',null);$('#user-avatar-remove').hidden=true;toast('已选择移除你的头像，保存档案后生效。');
  });
  $('#persona-avatar-remove').addEventListener('click', () => {
    state.avatarPending = null;
    paintAvatar($('#persona-avatar'), state.personaName, null);
    updateLogAvatar();
    $('#persona-avatar-remove').hidden = true;
    toast('已选择移除头像，点“保存档案”后生效。');
  });
  for (const id of ['persona-name', 'persona-tagline', 'persona-description']) {
    $('#' + id).addEventListener('input', updatePersonaCard);
  }
  $('#memory-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.memoryQuery = $('#memory-query').value.trim();
    state.memoryKind = $('#memory-kind').value;
    state.memoryStatus = $('#memory-status').value;
    state.memorySort = $('#memory-sort').value;
    logEvent('chat', '记忆筛选', [state.memoryQuery, MEMORY_KIND_LABELS[state.memoryKind], MEMORY_STATUS_LABELS[state.memoryStatus]].filter(Boolean).join(' · ') || '全部记忆');
    loadMemoryItems();
  });
  $('#memory-sort').addEventListener('change', () => {
    state.memorySort = $('#memory-sort').value;
    loadMemoryItems();
  });
  $('#memory-recall-form').addEventListener('submit', runRecallTest);
  $('#memory-maintain').addEventListener('click', maintainMemories);
  $('#memory-settings-form').addEventListener('submit', saveMemorySettings);
  $('#proactive-settings-form').addEventListener('submit', saveProactiveSettings);
  $('#proactive-test').addEventListener('click', testProactiveMessage);
  $('#memory-embedding-open').addEventListener('click', openEmbeddingSettings);
  $('#memory-refresh').addEventListener('click', loadMemoryPage);
  $('#memory-jobs-refresh').addEventListener('click', loadMemoryJobs);
  $('#memory-extract-now').addEventListener('click', extractMemoryNow);
  $('#memory-detail-close').addEventListener('click', () => {
    state.selectedMemoryId = null;
    $('#memory-detail').hidden = true;
    markSelectedMemoryCard();
  });
  $('#apply-correction').addEventListener('click', applyCorrection);
  $('#cancel-correction').addEventListener('click', closeCorrection);
  $('#memory-edit').addEventListener('click', (event) => { if (event.target === $('#memory-edit')) closeCorrection(); });
  $('#modal-ok').addEventListener('click', () => finishModal(modalValue()));
  $('#modal-cancel').addEventListener('click', () => finishModal(null));
  $('#modal').addEventListener('click', (event) => { if (event.target === $('#modal')) finishModal(null); });
  $('#modal-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') finishModal(modalValue()); });
}

initTheme();
state.logEntries = loadLogEntries();
renderTerm();
bind();
initLogResize();
new ResizeObserver(syncLogScrim).observe($('.composer-wrap'));
boot().catch((error) => showGate('启动失败：' + error.message));

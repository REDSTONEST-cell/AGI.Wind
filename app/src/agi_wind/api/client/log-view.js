// Human-readable view over the terminal log rows. Pure functions so the mapping can
// be tested without a browser; the drawer still keeps the raw rows for debugging.
export const LOG_LABELS = {
  gen: '对话', voice: '声音', memory: '记忆', agent: '工具',
  proactive: '主动', sys: '系统', api: '接口',
};

const TOOL_ACTIONS = {
  search_memory: { doing: '正在翻记忆', done: '翻了一下记忆', failed: '想翻记忆，但没成功' },
  search_stickers: { doing: '正在挑表情包', done: '挑了个表情包', failed: '想挑表情包，但没成功' },
  sticker_search: { doing: '正在挑表情包', done: '挑了个表情包', failed: '想挑表情包，但没成功' },
  send_sticker: { doing: '正在发表情包', done: '发了个表情包', failed: '想发表情包，但没成功' },
  web_search: { doing: '正在上网查', done: '上网查了一下', failed: '想上网查，但没查成' },
  web_fetch: { doing: '正在看网页', done: '打开网页看了看', failed: '想打开网页，但没看成' },
  read_file: { doing: '正在看文件', done: '看了文件', failed: '想看文件，但没看成' },
  list_files: { doing: '正在翻文件列表', done: '翻了翻文件', failed: '想翻文件，但没成功' },
  run_command: { doing: '正在运行命令', done: '运行了一条命令', failed: '想运行命令，但没成功' },
};

export function plainTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Math.max(0, Date.now() - date.getTime());
  if (diff < 45_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.max(1, Math.floor(diff / 60_000))} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

function compactCounter(text) {
  const match = text.match(/^(第 \d+ 轮(?: · 第 \d+ 步)?) · /);
  if (!match) return { counter: '', rest: text };
  const round = (match[1].match(/第 (\d+) 轮/) || [])[1];
  const step = (match[1].match(/第 (\d+) 步/) || [])[1];
  const counter = round ? `第${round}轮${step ? `·第${step}步` : ''}` : match[1];
  return { counter, rest: text.slice(match[0].length) };
}

function procRow(row) {
  const { counter, rest } = compactCounter(String(row.text || '').trim());
  const text = rest.replace(/^(回答前|回答后|独处) · /, '');
  const prefix = counter ? counter + ' · ' : '';
  if (row.tag === 'DONE') {
    if (/这次先不打扰/.test(text) && !/轮/.test(text)) return null;
    return { label: '这一轮', text: text || '回复完成', tone: 'done' };
  }
  if (row.tag === 'THINK') {
    if (/独处思考开始/.test(text)) return null;
    return { label: '她在想', text: prefix + (text || '想了想刚才的对话'), tone: 'think' };
  }
  if (row.tag === 'REPLY') {
    return text ? { label: '她说', text, tone: 'say' } : null;
  }
  if (row.tag === 'TOOL') {
    const match = text.match(/^(.+?) · (进行中|完成|失败|已跳过)(?: · (.*))?$/);
    if (!match) return { label: '她动手了', text: prefix + text, tone: 'tool' };
    const [, tool, status, summary] = match;
    const action = TOOL_ACTIONS[tool] || {
      doing: `正在用 ${tool}`,
      done: `用了 ${tool}`,
      failed: `用 ${tool} 时出错了`,
    };
    let line = status === '进行中' ? action.doing
      : status === '失败' ? action.failed
        : status === '已跳过' ? `跳过了 ${tool}`
          : action.done;
    if (summary && status === '完成' && /[\u4e00-\u9fa5]/.test(summary) && !summary.startsWith('正在')) {
      line += ' · ' + summary;
    }
    return { label: '她动手了', text: prefix + line, tone: status === '失败' ? 'error' : status === '进行中' ? 'doing' : 'tool' };
  }
  return null;
}

function systemRow(row) {
  if (row.level === 'error') return { label: '出错了', text: row.text, tone: 'error' };
  if (row.level === 'warn') {
    if (/gateway is reachable beyond this machine/i.test(row.text)) return null;
    return { label: '注意', text: row.text.replace(/^WARNING:\s*/i, ''), tone: 'warn' };
  }
  const label = LOG_LABELS[row.group] || '系统';
  let text = row.text;
  if (row.group === 'memory') {
    if (/^记忆提炼/.test(text)) text = '整理了一下关于你的记忆';
  } else if (row.group === 'proactive') {
    if (/主动消息已发送/.test(text)) text = '她主动发了一条消息';
  } else if (row.group === 'voice') {
    if (/^语音合成完成/.test(text)) text = '声音准备好了';
  } else if (row.group === 'agent') {
    // Tool detail already appears through the processing trace; raw search payloads
    // such as "sticker search query=… hits=2" are noise here.
    return null;
  } else {
    // API access lines, generation internals and misc backend INFO are for the raw view.
    return null;
  }
  return { label, text, tone: row.group };
}

function activityRow(row) {
  if (row.level === 'error') return { label: '出错了', text: row.text, tone: 'error' };
  const parts = String(row.text || '').split(' · ');
  const title = parts.shift() || '';
  const detail = parts.join(' · ');
  if (title === '发送消息') {
    const userText = detail.split(' · ')[0];
    return { label: '你', text: userText ? '你：' + userText : '你发了一条消息', tone: 'user' };
  }
  if (title === '收到主动消息') return { label: '她', text: '她主动来找你了', tone: 'proactive' };
  if (title === '生成已停止') return { label: '你', text: '你停止了这次回复', tone: 'user' };
  if (title === '生成失败') return { label: '出错了', text: '这次回复没有完成', tone: 'error' };
  if (title === '上下文已组装') {
    const count = Number((detail.match(/来源 (\d+) 条/) || [])[1] || 0);
    return count > 0 ? { label: '记忆', text: `想起了 ${count} 条相关记忆`, tone: 'memory' } : null;
  }
  return null;
}

/** Return a friendly row, or null when the row is technical noise. */
export function simpleLogRow(row) {
  if (!row || !String(row.text || '').trim()) return null;
  if (row.cat === 'proc') return procRow(row);
  if (row.cat === 'sys') return systemRow(row);
  if (row.cat === 'act') return activityRow(row);
  return null;
}

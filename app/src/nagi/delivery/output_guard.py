"""Bounded DSML compatibility and public-text filtering. No tool execution here."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MARKER=re.compile(r'<\s*(/?)\s*[|｜]+\s*DSML\s*[|｜]+\s*',re.I)
BLOCK=re.compile(r'<(calls|tool_calls|function_calls)\s*>(.*?)</\1\s*>',re.S)
INVOKE=re.compile(r'<invoke\s+name="([\w.-]{1,80})"\s*>(.*?)</invoke\s*>',re.S)
PARAM=re.compile(r'<parameter\s+name="([\w.-]{1,80})"(?:\s+string="(true|false)")?\s*>(.*?)</parameter\s*>',re.S)

def decode_dsml(text):
    if not MARKER.search(text): return text, []
    text=MARKER.sub(lambda m:'<'+m.group(1),text)
    blocks=list(BLOCK.finditer(text))
    if not blocks or len(blocks)>8: raise ValueError('incomplete DSML')
    calls=[]
    for block in blocks:
        body=block.group(2); pos=0
        for call in INVOKE.finditer(body):
            if body[pos:call.start()].strip(): raise ValueError('invalid DSML invoke')
            args={}; params=call.group(2); end=0
            for param in PARAM.finditer(params):
                if params[end:param.start()].strip() or param.group(1) in args: raise ValueError('invalid DSML parameter')
                raw=param.group(3)
                args[param.group(1)]=raw if param.group(2)=='true' else json.loads(raw)
                end=param.end()
            if params[end:].strip(): raise ValueError('incomplete DSML parameter')
            encoded=json.dumps(args,ensure_ascii=False)
            if len(encoded)>6000: raise ValueError('oversized DSML arguments')
            calls.append((call.group(1),encoded));pos=call.end()
        if body[pos:].strip(): raise ValueError('incomplete DSML invoke')
    rest=BLOCK.sub('',text)
    if re.search(r'</?(?:invoke|parameter|calls|tool_calls|function_calls)\b',rest) or not 1<=len(calls)<=8:
        raise ValueError('invalid DSML remainder')
    return rest.strip(),calls

HIDDEN=re.compile(r'^[\[【]\s*(?:(?:情绪|心情|表情包)\s*[:：][^\]】\n]{0,80}|消息时间\s*[:：]?\s*\d{4}-\d{2}-\d{2}[^\]】\n]{0,70}|表情包|开心|平静|难过|担心|撒娇|生气|惊讶|温柔|高兴|快乐|悲伤|害怕)\s*[\]】]')
PREFIXES=('[消息时间','【消息时间','[情绪','[心情','[表情包','【情绪','【心情','【表情包','[开心','[平静','[难过','[担心','[撒娇','[生气','[惊讶','[温柔','【开心','【平静','【难过','【担心','【撒娇','【生气','【惊讶','【温柔')

class VisibleTextGuard:
    def __init__(self): self.buffer=''; self.blocked=False
    def feed(self,text):
        if self.blocked: return ''
        self.buffer+=text; out=[]
        while self.buffer:
            if self.buffer[0]=='<':
                if MARKER.match(self.buffer):
                    self.blocked=True;self.buffer='';break
                if '>' not in self.buffer and len(self.buffer)<160: break
            if self.buffer[0] in '[【':
                match=HIDDEN.match(self.buffer)
                if match: self.buffer=self.buffer[match.end():];continue
                if len(self.buffer)<100 and '\n' not in self.buffer and not any(c in self.buffer for c in ']】'):
                    if any(p.startswith(self.buffer) or self.buffer.startswith(p) for p in PREFIXES): break
            out.append(self.buffer[0]);self.buffer=self.buffer[1:]
        return ''.join(out)
    def flush(self):
        text=self.buffer;self.buffer=''
        if self.blocked or re.match(r'^<\s*[/|｜]',text): return ''
        # At stream end, an incomplete prefix is ordinary visible text; only a
        # complete hidden directive is removed by feed().
        return text

WEEKDAYS=('周一','周二','周三','周四','周五','周六','周日')

def _aware(value):
    if value.tzinfo is None: return value.replace(tzinfo=timezone.utc)
    return value

def _event_text(event, limit=48):
    content=event.get('content')
    if isinstance(content,dict):
        text=content.get('text') or content.get('caption') or ''
    else:
        text=content or ''
    text=' '.join(str(text).split())
    return text[:limit]+('…' if len(text)>limit else '')

def _relative(seconds):
    seconds=max(0,int(seconds))
    if seconds<60: return '刚刚'
    if seconds<3600: return f'{seconds//60} 分钟前'
    if seconds<86400: return f'{seconds//3600} 小时前'
    return f'{seconds//86400} 天前'

def _day_label(local, today):
    delta=(today-local.date()).days
    if delta==0: return '今天'
    if delta==1: return '昨天'
    if delta==2: return '前天'
    if local.year==today.year: return f'{local.month}月{local.day}日'
    return f'{local.year}年{local.month}月{local.day}日'

def _period(hour):
    if hour<5: return '凌晨'
    if hour<8: return '清晨'
    if hour<11: return '上午'
    if hour<13: return '中午'
    if hour<18: return '下午'
    if hour<23: return '晚上'
    return '深夜'

def time_context(events=(), now=None, timezone_name='Asia/Shanghai'):
    """Give the model an unambiguous clock and a compact, ordered timeline.

    The prompt must be rebuilt for every turn: the model is stateless and its
    own sense of "now" is otherwise whatever it learned during training.
    """
    zone=ZoneInfo(timezone_name); now=_aware(now or datetime.now(timezone.utc))
    local_now=now.astimezone(zone)
    today=local_now.date()
    utc_offset=local_now.strftime('%z')
    utc_label='UTC'+utc_offset[:3]+':'+utc_offset[3:]
    lines=[
        '【时间感知】',
        '当前时刻：'+local_now.strftime('%Y-%m-%d %H:%M:%S')+'（'+WEEKDAYS[local_now.weekday()]+
        '，'+timezone_name+'，'+utc_label+'，'+_period(local_now.hour)+'）。',
    ]
    stamps=[]
    for event in events:
        if event.get('role') not in ('user','assistant'): continue
        stamp=event.get('recorded_at')
        if not stamp: continue
        try:
            dt=_aware(datetime.fromisoformat(str(stamp).replace('Z','+00:00')))
        except (TypeError,ValueError):
            continue
        stamps.append((dt,event))
    stamps.sort(key=lambda item:item[0])
    if stamps:
        last,last_event=stamps[-1]
        last_local=last.astimezone(zone)
        who='主人' if last_event.get('role')=='user' else '你'
        gap=_relative((now-last).total_seconds())
        lines.append('本轮历史最近一条消息：'+_day_label(last_local,today)+' '+
                     last_local.strftime('%H:%M:%S')+'（'+gap+'，'+who+'）。')
        users=[item for item in stamps if item[1].get('role')=='user']
        if users and users[-1][1] is not last_event:
            user_dt,user_event=users[-1]
            user_local=user_dt.astimezone(zone)
            lines.append('最近一次用户发言：'+_day_label(user_local,today)+' '+
                         user_local.strftime('%H:%M:%S')+'（'+
                         _relative((now-user_dt).total_seconds())+'，'+
                         _event_text(user_event,24)+'）。')
        if (now-last).total_seconds()>=300:
            lines.append('距上一条消息已经有一段时间；如果用户像是刚回来，自然接话即可，不要刻意报出精确分钟数。')
        lines.append('最近对话时间线（按时间先后，仅供理解顺序和间隔）：')
        for dt,event in stamps[-12:]:
            local=dt.astimezone(zone)
            who='主人' if event.get('role')=='user' else '你'
            speaker=event.get('speaker')
            if speaker: who=str(speaker)
            text=_event_text(event)
            lines.append('- '+_day_label(local,today)+' '+local.strftime('%H:%M:%S')+
                         ' '+who+'：'+(text or '（非文字内容）'))
    else:
        lines.append('这是本次会话的第一条消息，没有更早的对话时间线。')
    lines.append('使用规则：用户问现在几点、今天几号、隔了多久时，依据上面的当前时刻和时间线自然回答；'
                 '不要编造时间，也不要把历史消息或工具资料里的旧时间当成现在。除非用户主动问时间，'
                 '否则不要主动报时，也不要在回复里输出时间前缀、字段名、方括号标签或 JSON。')
    return '\n'.join(lines)

def public_text(text):
    guard=VisibleTextGuard()
    return guard.feed(str(text or ''))+guard.flush()

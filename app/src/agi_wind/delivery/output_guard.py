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

def time_context(events=(), now=None):
    zone=ZoneInfo('Asia/Shanghai'); now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: now=now.replace(tzinfo=timezone.utc)
    lines=['【可信时间上下文】当前北京时间：'+now.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S %A')+'；时区 Asia/Shanghai (UTC+08:00)。']
    stamps=[]
    for event in events:
        stamp=event.get('recorded_at')
        if stamp:
            try:
                dt=datetime.fromisoformat(str(stamp).replace('Z','+00:00'))
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                stamps.append((dt,event.get('role','unknown')))
            except (TypeError,ValueError): pass
    if stamps:
        last,role=max(stamps)
        lines.append('本轮历史中最近一条消息：'+last.astimezone(zone).isoformat()+'，距现在约 '+str(max(0,int((now-last).total_seconds())))+' 秒；发言角色 '+role+'。')
    lines.append('以上时间与日期是供理解上下文的元数据，绝不输出字段名、方括号标签、JSON或时间前缀；仅在用户询问日期或时长时自然表达。历史及工具资料中的过期时间不能当成当前时间。')
    return '\n'.join(lines)

def public_text(text):
    guard=VisibleTextGuard()
    return guard.feed(str(text or ''))+guard.flush()

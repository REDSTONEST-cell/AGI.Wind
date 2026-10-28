// Per-gateway, per-conversation cursors survive watcher replacement on this page.
export class LiveCursors {
  constructor(limit=64) { this.values=new Map(); this.limit=limit; }
  has(key) { return this.values.has(key); }
  get(key) { return this.values.get(key) || 0; }
  seed(key, sequence) {
    if (this.has(key)) return;
    this.values.set(key,Number.isSafeInteger(sequence)&&sequence>=0?sequence:0);
    if(this.values.size>this.limit)this.values.delete(this.values.keys().next().value);
  }
  accept(key, sequence) {
    if(!Number.isSafeInteger(sequence)||sequence<=this.get(key))return false;
    this.seed(key,0);
    this.values.set(key,sequence);
    return true;
  }
}

export function idleCompletion(payload={}) {
  const rounds=Number(payload.rounds||0),steps=Number(payload.steps||0);
  const reason=String(payload.reason||'');
  const failed=Boolean(payload.error_code)||/^(?:cognition_failed|cognition_timeout|invalid_output|model_unconfigured)$/.test(reason)
    || (/^[A-Z][A-Z_]+$/.test(reason)&&!reason.endsWith('CANCELLED'));
  const sent=payload.decision==='reach_out';
  if(!rounds&&!steps&&!sent&&!failed)return null;
  const counts=rounds||steps?` · ${rounds} 轮 / ${steps} 步`:'';
  return {sent,failed,text:(failed?'自主任务失败':sent?'自主任务结束 · 已发出消息':'自主任务结束 · 本次保持安静')+counts};
}

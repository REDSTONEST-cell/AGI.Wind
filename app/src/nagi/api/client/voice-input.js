/** Dictation reuses the authenticated local ASR transport; it never submits chat. */
export function createVoiceInput(client, hooks) {
  let current = null, generation = 0;
  const state = (label) => hooks.state?.(!!current, label);
  function valid(run) {
    if (current !== run || run.generation !== generation) return false;
    if (hooks.conversationId() !== run.conversationId) { void cancel(); return false; }
    return true;
  }
  function show(run) {
    if (valid(run)) hooks.text([...run.finals, run.partial].filter(Boolean).join(' '));
  }
  function stopCapture(run) {
    run.stream?.getTracks().forEach(t => { t.onended = null; t.stop(); });
    if (run.capture) { run.capture.port.onmessage = null; run.capture.disconnect(); }
    run.source?.disconnect();
    run.stream = run.capture = run.source = null;
  }
  async function cancel(label = '语音输入已结束，可检查文字后发送') {
    const run = current; current = null; generation += 1;
    if (!run) return;
    clearInterval(run.ping); clearInterval(run.silence); clearTimeout(run.limit);
    clearTimeout(run.finishTimer); clearTimeout(run.handshakeTimer);
    run.rejectReady?.(new Error('录音已取消')); run.rejectReady = null;
    stopCapture(run);
    if (run.socket) { run.socket.onclose = run.socket.onerror = run.socket.onmessage = null; run.socket.close(); }
    const closing = run.context?.close().catch(() => {});
    state(label);
    await closing;
  }
  async function finish() {
    const run = current;
    if (!run || run.finishing) return;
    if (!run.ready) { await cancel(); return; }
    run.finishing = true;
    stopCapture(run); state('正在确认最后一句…');
    // Feed real-time silence for the existing VAD endpoint; release the physical mic immediately.
    run.silence = setInterval(() => {
      if (valid(run) && run.socket.readyState === WebSocket.OPEN) run.socket.send(new Float32Array(640).buffer);
    }, 40);
    run.finishTimer = setTimeout(() => { if (valid(run)) { show(run); void cancel(); } }, 1400);
  }
  async function start() {
    if (current) return;
    const run = {generation:++generation, finals:[], partial:'', lastUtterance:0};
    current = run; state('正在连接麦克风…');
    try {
      run.conversationId = await hooks.prepare();
      if (current !== run) return;
      if (!run.conversationId) { await cancel(''); return; }
      run.context = new AudioContext({sampleRate:16000, latencyHint:'interactive'});
      await run.context.resume();
      if (!valid(run)) return;
      const stream = await hooks.openMicrophone();
      if (!valid(run)) { stream.getTracks().forEach(t => t.stop()); return; }
      run.stream = stream;
      if (run.context.sampleRate !== 16000) throw new Error('当前浏览器不支持 16kHz 语音输入');
      const issued = await client.request('/api/v1/call/ticket', {method:'POST',
        body:{conversation_id:run.conversationId}, signal:AbortSignal.timeout(5000)});
      if (!valid(run)) return;
      const url = new URL(client.url('/api/v1/call/stream'), location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = run.socket = new WebSocket(url);
      await new Promise((resolve, reject) => {
        run.rejectReady = reject;
        run.handshakeTimer = setTimeout(() => reject(new Error('语音识别连接超时，请检查本地 ASR 服务')), 7000);
        socket.onopen = () => socket.send(JSON.stringify({ticket:issued.ticket}));
        socket.onerror = () => reject(new Error('无法连接语音识别服务'));
        socket.onclose = () => reject(new Error('语音识别连接已关闭'));
        socket.onmessage = event => {
          try {
            const value = JSON.parse(event.data);
            if (value.type === 'ready') { clearTimeout(run.handshakeTimer); run.rejectReady = null; resolve(); }
            else if (value.type === 'error') reject(new Error(value.message || '语音识别不可用'));
          } catch { reject(new Error('语音识别数据异常')); }
        };
      });
      if (!valid(run)) return;
      socket.onclose = () => { if (valid(run)) { hooks.error('语音识别连接断开，已识别文字保留'); void cancel(); } };
      socket.onerror = () => { if (valid(run)) { hooks.error('语音识别连接异常'); void cancel(); } };
      socket.onmessage = event => {
        if (!valid(run) || typeof event.data !== 'string') return;
        try {
          const value = JSON.parse(event.data);
          if (value.type === 'partial') { run.partial = value.text || ''; show(run); }
          else if (value.type === 'final' && value.utterance > run.lastUtterance) {
            run.lastUtterance = value.utterance;
            if (value.text) run.finals.push(value.text);
            run.partial = ''; show(run);
          } else if (value.type === 'error') { hooks.error(value.message || '语音识别失败'); void cancel(); }
        } catch { hooks.error('语音识别数据异常'); void cancel(); }
      };
      await run.context.audioWorklet.addModule(new URL('./call-capture.js?v=20261026-call1', import.meta.url));
      if (!valid(run)) return;
      run.source = run.context.createMediaStreamSource(run.stream);
      run.capture = new AudioWorkletNode(run.context, 'agi-call-capture');
      run.capture.port.onmessage = event => {
        if (!valid(run) || run.finishing) return;
        if (socket.bufferedAmount > 64000) { hooks.error('音频连接积压，录音已停止'); void cancel(); return; }
        if (socket.readyState === WebSocket.OPEN) socket.send(event.data);
      };
      run.source.connect(run.capture); run.capture.connect(run.context.destination);
      for (const track of run.stream.getTracks()) track.onended = () => { if (valid(run)) void finish(); };
      run.ping = setInterval(() => { if (valid(run) && socket.readyState === WebSocket.OPEN) socket.send('{"type":"ping"}'); }, 10000);
      run.limit = setTimeout(() => void finish(), 120000);
      run.ready = true; state('正在听你说 · 再点麦克风结束（最多 2 分钟）');
    } catch (error) {
      if (current === run) { hooks.error(error); await cancel('语音输入未连接'); }
    }
  }
  return {start, finish, cancel, get active() { return !!current; }};
}

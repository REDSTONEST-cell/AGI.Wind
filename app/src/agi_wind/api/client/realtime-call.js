import { PcmPlayer, SpeechChunks } from './call-audio.js?v=20261026-call1';
/** A call owns its own epochs, audio and chat request, so late results cannot resume it. */
export function createRealtimeCall(client, hooks) {
  const button = document.querySelector('#call-toggle');
  const panel = document.querySelector('#call-panel');
  const status = document.querySelector('#call-status');
  const caption = document.querySelector('#call-caption');
  const mute = document.querySelector('#call-mute');
  let active = false, starting = false, muted = false, session = 0, epoch = 0;
  let socket, stream, context, capture, source, player, ping, turn;
  let conversationId = '', callId = '', rate = 24000, lastUtterance = 0;
  let speechStarted = false, speechCredits = 0, pendingSpeech = [];
  let cancelling = Promise.resolve();
  const send = (value) => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const label = (text) => { status.textContent = text; };
  const log = (text, data = {}) => hooks.log?.(text, data);
  function drainSpeech() {
    while (active && !muted && speechCredits > 0 && pendingSpeech.length) {
      send({type:'speak', epoch, text:pendingSpeech.shift()});
      speechCredits -= 1;
    }
  }
  async function cancelGeneration(previous) {
    if (!previous) return;
    previous.cancelled = true;
    previous.controller.abort();
    previous.view?.finish('cancelled');
    // Submission is not aborted: if accepted during an interruption, consume its ID and cancel it.
    try {
      const accepted = await previous.accepting;
      if (accepted?.generation_id) await client.request('/api/v1/generations/' + accepted.generation_id + '/interrupt', {
        method:'POST', body:{command_id:crypto.randomUUID()}, signal:AbortSignal.timeout(5000),
      });
    } catch (error) { log('通话取消请求结束', {code:error.code || error.name}); }
  }
  function interrupt() {
    epoch += 1;
    pendingSpeech = []; speechCredits = 0;
    player?.stop();
    hooks.stopAudio?.();
    const previous = turn; turn = null;
    send({type:'cancel', epoch});
    cancelling = Promise.allSettled([cancelling, cancelGeneration(previous)]);
    return cancelling;
  }
  async function reply(text, version, expectedSession) {
    await cancelling;
    if (!active || expectedSession !== session || version !== epoch || muted) return;
    const current = {controller:new AbortController(), cancelled:false, accepted:null};
    turn = current;
    current.view = hooks.beginTurn(text);
    label('正在回应…');
    const chunks = new SpeechChunks();
    let received = '', terminal = false, first = true;
    const started = performance.now();
    const speak = (texts) => {
      if (active && !current.cancelled && version === epoch) { pendingSpeech.push(...texts); drainSpeech(); }
    };
    try {
      current.accepting = client.chat({request_id:crypto.randomUUID(), conversation_id:conversationId,
        input:{text, asset_ids:[]}, output:{voice:'off', realtime:true}, device_id:callId});
      const accepted = await current.accepting;
      current.accepted = accepted;
      if (current.cancelled || !active || expectedSession !== session || version !== epoch) return;
      await client.stream(accepted.events_url, {signal:current.controller.signal, onEvent(event) {
        if (current.cancelled || !active || version !== epoch) return;
        const payload = event.data?.payload || {};
        if (event.type === 'text.delta' && typeof payload.text === 'string') {
          if (first) { first = false; log('通话首字', {elapsed_ms:Math.round(performance.now()-started)}); }
          received += payload.text;
          current.view.update(received);
          speak(chunks.feed(payload.text));
        } else if (event.type === 'text.completed') {
          speak(chunks.flush());
        } else if (event.type === 'generation.completed') {
          terminal = true; speak(chunks.flush()); current.view.finish('completed');
        } else if (event.type === 'generation.failed' || event.type === 'generation.cancelled') {
          terminal = true; current.view.finish(event.type.endsWith('failed') ? 'failed' : 'cancelled');
          if (event.type.endsWith('failed')) label('回复失败，请再说一次');
        } else if (event.type === 'sticker.sent' && payload.attachment) {
          current.view.sticker?.(payload.attachment);
        } else if (event.type.startsWith('reasoning.') || event.type.startsWith('tool.')) {
          hooks.trace?.(accepted.generation_id, event.type, payload);
        }
      }});
      if (!terminal && !current.cancelled) throw new Error('回复连接提前结束');
    } catch (error) {
      if (!current.cancelled && active && expectedSession === session) {
        current.view.finish('failed'); label('回复中断，请再说一次');
        log('通话回复失败', {code:error.code || error.name});
        // A broken SSE connection must not leave an invisible generation running.
        await cancelGeneration(current);
      }
    } finally {
      if (turn === current && (current.cancelled || terminal)) turn = null;
    }
  }
  function message(event, expectedSession) {
    if (expectedSession !== session || !active) return;
    if (event.data instanceof ArrayBuffer) {
      if (event.data.byteLength < 4) return;
      const packetEpoch = new DataView(event.data).getUint32(0, true);
      if (packetEpoch === epoch) { player.push(new Uint8Array(event.data,4), rate); label('她正在说话…'); }
      return;
    }
    const value = JSON.parse(event.data);
    if (value.type === 'speech_start' && !muted) {
      void interrupt(); speechStarted = true;
      label('正在听你说…'); caption.textContent = '';
    } else if (value.type === 'partial' && !muted) {
      caption.textContent = value.text;
    } else if (value.type === 'final' && !muted && value.utterance > lastUtterance) {
      lastUtterance = value.utterance;
      caption.textContent = value.text;
      // A final may arrive without a VAD start; it must still replace the old turn.
      if (!speechStarted) void interrupt();
      speechStarted = false;
      if (/^(?:\/\s*)?(?:stop|停止)[。.!！]?$/i.test(value.text.trim())) { void stop(); return; }
      void reply(value.text, epoch, expectedSession);
    } else if (value.type === 'speech_reset' && value.epoch === epoch) {
      speechCredits = Math.min(16, Math.max(0, value.credits || 0)); drainSpeech();
    } else if (value.type === 'speech_credit' && value.epoch === epoch) {
      speechCredits = Math.min(16, speechCredits + Math.max(0, value.credits || 0)); drainSpeech();
    } else if (value.type === 'audio_start' && value.epoch === epoch) {
      rate = value.sample_rate || 24000;
    } else if (value.type === 'speech_end' && !turn && !muted) {
      label('正在聆听…');
    } else if (value.type === 'error') {
      label(value.message || '通话发生错误'); log('通话服务提示', {code:value.code});
    }
  }
  async function start() {
    if (active || starting) return;
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      hooks.error('麦克风需要安全连接：本机使用 localhost，手机或局域网请使用 HTTPS'); return;
    }
    starting = true; const expectedSession = ++session;
    button.setAttribute('aria-pressed','true'); button.textContent = '挂断'; panel.hidden = false;
    label('连接语音服务…'); caption.textContent = ''; muted = false; lastUtterance = 0; epoch = 0; speechStarted = false;
    pendingSpeech = []; speechCredits = 0;
    try {
      const selected = await hooks.prepare();
      if (!selected || expectedSession !== session) { if (expectedSession === session) await stop(); return; }
      context = new AudioContext({sampleRate:16000, latencyHint:'interactive'});
      await context.resume();
      const health = await client.request('/api/v1/call/status', {signal:AbortSignal.timeout(6000)});
      if (!health.available) throw new Error('语音服务未就绪，请检查本地识别服务和 Breeze');
      if (expectedSession !== session) return;
      conversationId = selected;
      const issued = await client.request('/api/v1/call/ticket', {method:'POST', body:{conversation_id:conversationId}, signal:AbortSignal.timeout(5000)});
      if (expectedSession !== session) return;
      callId = issued.call_id;
      if (!callId) throw new Error('通话身份无效，请刷新页面重连');
      const url = new URL(client.url('/api/v1/call/stream'), location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(url); socket.binaryType = 'arraybuffer';
      await new Promise((resolve,reject) => {
        const timer = setTimeout(() => reject(new Error('通话连接超时')), 7000);
        socket.onopen = () => socket.send(JSON.stringify({ticket:issued.ticket}));
        socket.onerror = () => { clearTimeout(timer); reject(new Error('无法连接通话服务')); };
        socket.onclose = () => { clearTimeout(timer); reject(new Error('通话连接已关闭')); };
        socket.onmessage = (event) => {
          if (typeof event.data === 'string' && JSON.parse(event.data).type === 'ready') { clearTimeout(timer); resolve(); }
          else if (typeof event.data === 'string') { clearTimeout(timer); reject(new Error(JSON.parse(event.data).message || '识别服务未就绪')); }
        };
      });
      if (expectedSession !== session) return;
      // The socket can close while the browser permission prompt is still open.
      socket.onclose = () => { if (expectedSession === session) { hooks.error('通话连接已断开'); void stop(); } };
      socket.onerror = () => { if (expectedSession === session) void stop(); };
      label('请允许麦克风访问…');
      const input = hooks.openMicrophone ? await hooks.openMicrophone() : await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}, video:false});
      if (expectedSession !== session) { input.getTracks().forEach(track=>track.stop()); return; }
      stream = input;
      if (context.sampleRate !== 16000) throw new Error('当前浏览器不支持 16kHz 通话音频');
      await context.audioWorklet.addModule(new URL('./call-capture.js?v=20261026-call1', import.meta.url));
      if (expectedSession !== session) return;
      player = new PcmPlayer(context); active = true;
      hooks.stopAudio?.(); hooks.active?.(true);
      socket.onmessage = (event) => { try { message(event, expectedSession); } catch { label('通话数据异常'); void stop(); } };
      socket.onclose = () => { if (expectedSession === session) { hooks.error('通话连接已断开'); void stop(); } };
      socket.onerror = () => { if (expectedSession === session) void stop(); };
      source = context.createMediaStreamSource(stream);
      capture = new AudioWorkletNode(context, 'agi-call-capture');
      capture.port.onmessage = (event) => {
        if (!active || muted || expectedSession !== session) return;
        // Browser AEC stays enabled while capturing user speech during playback.
        if (hooks.conversationId() !== conversationId) { void stop(); return; }
        if (socket.bufferedAmount > 64000) { hooks.error('音频连接积压，通话已挂断'); void stop(); return; }
        if (socket.readyState === WebSocket.OPEN) socket.send(event.data);
      };
      source.connect(capture); capture.connect(context.destination);
      for (const track of stream.getTracks()) track.onended = () => { if (expectedSession === session) void stop(); };
      ping = setInterval(() => {
        send({type:'ping'});
        if (active && !muted && !turn && !player.playing) label('正在聆听…');
      }, 10000);
      mute.disabled = false; mute.textContent = '静音'; mute.setAttribute('aria-pressed','false');
      label('正在聆听…'); log('通话已接通');
    } catch (error) {
      if (expectedSession === session) { hooks.error(hooks.formatError ? hooks.formatError(error) : error.message); await stop(); }
    } finally { if (expectedSession === session) starting = false; }
  }
  async function stop() {
    if (!active && !starting && !context) return;
    session += 1; active = false; starting = false;
    const cancelled = interrupt();
    clearInterval(ping);
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
    stream?.getTracks().forEach(track=>{track.onended=null; track.stop();}); stream = null;
    if (capture) { capture.port.onmessage=null; capture.disconnect(); capture = null; }
    source?.disconnect(); source = null;
    const closing = context; context = null; player = null;
    mute.disabled = true; muted = false; panel.hidden = true;
    button.textContent = '通话'; button.setAttribute('aria-pressed','false'); hooks.active?.(false);
    log('通话已挂断');
    if (closing) await closing.close().catch(()=>{});
    await cancelled;
  }
  button.addEventListener('click', () => { if (active || starting) void stop(); else void start(); });
  document.querySelector('#call-hangup').addEventListener('click', () => void stop());
  mute.addEventListener('click', () => {
    muted = !muted;
    stream?.getAudioTracks().forEach(track=>{track.enabled = !muted;});
    interrupt(); speechStarted = false; send({type:'reset'}); caption.textContent = '';
    label(muted ? '麦克风已静音' : '正在聆听…');
    mute.textContent = muted ? '取消静音' : '静音'; mute.setAttribute('aria-pressed',String(muted));
  });
  window.addEventListener('pagehide', () => void stop());
  return {start, stop, interrupt, get active(){return active || starting;}, get conversationId(){return conversationId;}};
}

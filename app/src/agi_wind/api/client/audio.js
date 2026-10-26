/**
 * Custom voice-message player used by chat history and live assistant turns.
 * Keeps one global audio element playing and never stores blobs in localStorage.
 */

const WAVE_HEIGHTS = [7, 12, 18, 10, 22, 15, 9, 19, 25, 13, 8, 17, 23, 11, 16, 21, 9, 14, 20, 12, 24, 10, 16, 8];
let activePlayer = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// SVG glyphs avoid the play triangle being rendered as a colour emoji on some platforms.
const GLYPHS = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true" style="width:14px;height:14px;margin-left:2px;fill:currentColor;stroke:none"><path d="M7 4.8v14.4a1 1 0 0 0 1.5.86l12-7.2a1 1 0 0 0 0-1.72l-12-7.2A1 1 0 0 0 7 4.8z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true" style="width:14px;height:14px;fill:currentColor;stroke:none"><rect x="6" y="4.5" width="4" height="15" rx="1.2"/><rect x="14" y="4.5" width="4" height="15" rx="1.2"/></svg>',
};

function setGlyph(button, name) {
  button.innerHTML = GLYPHS[name];
}

function formatDuration(seconds) {
  const value = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return minutes + ':' + String(rest).padStart(2, '0');
}

function stateLabel(state, options = {}) {
  if (state === 'queued') return '语音排队中…';
  if (state === 'synthesizing') return '正在合成语音…';
  if (state === 'failed') return '语音生成失败' + (options.error_code ? ' · ' + options.error_code : '');
  if (state === 'skipped') return '已跳过语音';
  if (state === 'cancelled') return '语音已取消';
  return '';
}

function stopOther(player) {
  if (activePlayer && activePlayer !== player) activePlayer.stop();
}

export function stopAllAudio() {
  if (activePlayer) {
    activePlayer.stop();
    activePlayer = null;
  }
}

export function createAudioMessage(client, message, options = {}) {
  const root = node('div', 'audio-message');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', '语音消息');
  const play = node('button', 'audio-play');
  play.type = 'button';
  play.setAttribute('aria-label', '播放语音');
  setGlyph(play, 'play');
  const wave = node('div', 'audio-wave');
  wave.setAttribute('aria-hidden', 'true');
  const bars = WAVE_HEIGHTS.map((height, index) => {
    const bar = node('span', 'audio-bar');
    bar.style.height = height + 'px';
    bar.dataset.index = String(index);
    wave.append(bar);
    return bar;
  });
  const duration = node('span', 'audio-duration', '--:--');
  const status = node('span', 'audio-status');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  root.append(play, wave, duration, status);
  message.bubble.append(root);
  message.body.hidden = true;

  let audio = null;
  let pendingPlay = false;
  let objectUrl = null;
  let attachment = null;
  let currentState = 'queued';
  let destroyed = false;
  let loadToken = 0;
  let autoplayWanted = Boolean(options.autoplay);
  let autoplayTried = false;

  const release = () => {
    if (audio) {
      try { audio.pause(); } catch {}
      audio.removeAttribute('src');
      audio.load();
      audio = null;
    }
    if (objectUrl) {
      try { URL.revokeObjectURL(objectUrl); } catch {}
      objectUrl = null;
    }
  };

  const setProgress = (ratio) => {
    const value = Math.max(0, Math.min(1, Number(ratio) || 0));
    const activeBars = Math.round(value * bars.length);
    bars.forEach((bar, index) => bar.classList.toggle('on', index < activeBars));
  };

  const setStatus = (text, state) => {
    root.dataset.state = state || currentState;
    status.textContent = text;
    status.hidden = !text;
    const playable = state === 'ready';
    play.disabled = !playable && state !== 'paused';
    root.classList.toggle('audio-loading', state === 'queued' || state === 'synthesizing');
    root.classList.toggle('audio-failed', state === 'failed' || state === 'skipped' || state === 'cancelled');
  };

  const loadAudio = () => {
    const assetId = attachment?.asset_id;
    if (!assetId) { setStatus('语音不可用', 'failed'); play.disabled = true; return; }
    const expected = attachment?.duration_ms ? attachment.duration_ms / 1000 : 0;
    duration.textContent = expected ? formatDuration(expected) : '--:--';
    setStatus('', 'ready');
    const token = ++loadToken;
    client.assetObjectUrl(assetId).then((url) => {
      if (destroyed || token !== loadToken) return;
      release();
      objectUrl = String(url).startsWith('blob:') ? url : null;
      audio = new Audio(url);
      audio.preload = 'metadata';
      audio.addEventListener('error', () => {
        if (destroyed) return;
        setStatus('语音加载失败，点击重试', 'ready');
        release();
      });
      audio.addEventListener('loadedmetadata', () => {
        if (destroyed || !audio) return;
        duration.textContent = formatDuration(audio.duration);
      });
      audio.addEventListener('timeupdate', () => {
        if (!audio) return;
        const total = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
        setProgress(total ? audio.currentTime / total : 0);
        duration.textContent = formatDuration(total || expected);
      });
      audio.addEventListener('play', () => {
        setGlyph(play, 'pause');
        play.setAttribute('aria-label', '暂停语音');
        setStatus('', 'ready');
      });
      audio.addEventListener('pause', () => {
        setGlyph(play, 'play');
        play.setAttribute('aria-label', '播放语音');
        if (audio && !audio.ended && activePlayer === controller) setStatus('', 'ready');
      });
      audio.addEventListener('ended', () => {
        setGlyph(play, 'play');
        play.setAttribute('aria-label', '播放语音');
        duration.textContent = formatDuration(expected || 0);
        setProgress(0);
        if (activePlayer === controller) activePlayer = null;
      });
      play.disabled = false;
      if (pendingPlay) {
        pendingPlay = false;
        stopOther(controller);
        activePlayer = controller;
        audio.play().catch(() => setStatus('点击播放', 'ready'));
      }
      if (autoplayWanted && !autoplayTried) {
        autoplayTried = true;
        stopOther(controller);
        activePlayer = controller;
        audio.play().catch(() => setStatus('点击播放', 'ready'));
      }
    }).catch((error) => {
      if (destroyed || token !== loadToken) return;
      setStatus(error && error.status === 401 ? '语音需要重新登录' : '语音读取失败，点击重试', 'ready');
    });
  };

  const toggle = () => {
    if (currentState !== 'ready') return;
    if (!audio) { pendingPlay = true; loadAudio(); return; }
    if (audio.paused) {
      stopOther(controller);
      activePlayer = controller;
      audio.play().catch(() => setStatus('点击播放', 'ready'));
    } else {
      audio.pause();
    }
  };

  const update = (next = {}) => {
    if (destroyed) return;
    if (next.attachment) attachment = { ...attachment, ...next.attachment };
    const nextState = next.state || currentState;
    currentState = nextState;
    root.dataset.voiceId = next.voice_id || attachment?.voice_id || '';
    root.dataset.emotion = next.emotion || attachment?.emotion || '';
    if (Number.isFinite(Number(next.autoplay))) autoplayWanted = Boolean(next.autoplay);
    if (next.autoplay === true) autoplayWanted = true;
    if (next.autoplay === false) autoplayWanted = false;

    if (nextState === 'ready') {
      loadAudio();
      return;
    }

    release();
    play.disabled = true;
    setGlyph(play, 'play');
    play.setAttribute('aria-label', '播放语音');
    duration.textContent = '--:--';
    setProgress(0);
    setStatus(stateLabel(nextState, next), nextState);
  };

  play.addEventListener('click', toggle);

  const controller = {
    root,
    update,
    destroy() {
      destroyed = true;
      loadToken += 1;
      release();
      if (activePlayer === controller) activePlayer = null;
      root.remove();
    },
    stop() {
      if (audio && !audio.paused) audio.pause();
    },
    get attachment() { return attachment; },
    get state() { return currentState; },
  };
  update(options);
  return controller;
}

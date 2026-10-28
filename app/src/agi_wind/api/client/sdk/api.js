/** AGI.Wind browser SDK: session, JSON API and SSE stream helpers (no build step). */

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code);
    this.code = code || 'ERROR';
    this.status = status || 0;
  }
}

function parseEventBlock(block) {
  const lines = block.split('\n');
  let id = null;
  let type = 'message';
  const data = [];
  for (const line of lines) {
    if (line.startsWith('id:')) id = line.slice(3).trim();
    else if (line.startsWith('event:')) type = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try {
    return { id, type, data: JSON.parse(data.join('\n')) };
  } catch {
    return { id, type, data: data.join('\n') };
  }
}

export class AgiWindClient {
  constructor(baseUrl = '') {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.csrf = '';
    this.bearer = '';
    this._assetCache = new Map();
  }

  url(path) {
    return this.baseUrl + path;
  }

  /** Point the client at another gateway (server/client split). */
  setBase(baseUrl) {
    this.baseUrl = (baseUrl || '').replace(/\/$/, '');
    this._assetCache.clear();
  }

  /** Cross-origin / standalone mode authenticates every request with the client key. */
  useBearer(key) {
    this.bearer = key || '';
  }

  clearBearer() {
    this.bearer = '';
  }

  authHeaders() {
    return this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {};
  }

  async request(path, options = {}) {
    const method = options.method || 'GET';
    const headers = { Accept: 'application/json', ...this.authHeaders(), ...(options.headers || {}) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && method !== 'HEAD' && !this.bearer) {
      if (this.csrf) headers['X-AGI-CSRF'] = this.csrf;
    }
    let response;
    try {
      response = await fetch(this.url(path), {
        method,
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: this.bearer ? 'omit' : 'same-origin',
        cache: 'no-store',
        signal: options.signal,
      });
    } catch (error) {
      throw new ApiError('NETWORK', '无法连接服务：' + error.message, 0);
    }
    if (response.status === 204) return null;
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = text;
    }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : null;
      throw new ApiError(detail ? detail.code : 'HTTP_' + response.status, detail ? detail.message : '请求未完成', response.status);
    }
    return payload;
  }

  /** Upload an image/audio file; returns the asset record ({id, mime, bytes, sha256}). */
  async uploadAsset(file) {
    const form = new FormData();
    form.append('file', file, file.name || 'upload.bin');
    const headers = this.authHeaders();
    if (!this.bearer && this.csrf) headers['X-AGI-CSRF'] = this.csrf;
    let response;
    try {
      response = await fetch(this.url('/api/v1/assets'), {
        method: 'POST',
        body: form,
        headers,
        credentials: this.bearer ? 'omit' : 'same-origin',
      });
    } catch (error) {
      throw new ApiError('NETWORK', '无法连接服务：' + error.message, 0);
    }
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : null;
      throw new ApiError(detail ? detail.code : 'HTTP_' + response.status, detail ? detail.message : '上传失败', response.status);
    }
    return payload;
  }

  assetUrl(assetId) {
    return this.url('/api/v1/assets/' + encodeURIComponent(assetId));
  }

  /** Load any asset as an authenticated blob URL (cookies for same-origin, Bearer for split mode). */
  async assetObjectUrl(assetId) {
    if (this._assetCache.has(assetId)) return this._assetCache.get(assetId);
    const response = await fetch(this.assetUrl(assetId), {
      headers: this.authHeaders(),
      credentials: this.bearer ? 'omit' : 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) throw new ApiError('ASSET_UNAVAILABLE', '资源读取失败', response.status);
    const url = URL.createObjectURL(await response.blob());
    this._assetCache.set(assetId, url);
    return url;
  }

  /** Read the browser session's CSRF token; returns null when not signed in. */
  async session() {
    try {
      const state = await this.request('/api/v1/client/session');
      this.csrf = (state && state.csrf_token) || '';
      return state;
    } catch (error) {
      if (error.status === 401) return null;
      throw error;
    }
  }

  /**
   * Ask the server for a loopback-only automatic session.
   * Returns null when the server requires an explicit key (non-local access).
   */
  async bootstrap() {
    try {
      const state = await this.request('/api/v1/client/bootstrap', { method: 'POST' });
      this.csrf = state.csrf_token || '';
      return state;
    } catch (error) {
      if (error.status === 401 || error.status === 403) return null;
      throw error;
    }
  }

  /** Same-origin key -> HttpOnly cookie session (no token in browser storage). */
  async keySession(key) {
    const state = await this.request('/api/v1/client/key-session', {
      method: 'POST',
      body: { key },
    });
    this.csrf = state.csrf_token || '';
    return state;
  }

  /** Bearer token -> one-time ticket -> HttpOnly cookie session. Token stays in memory only. */
  async login(token) {
    const issued = await this.request('/api/v1/client/launch', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
    });
    const state = await this.request('/api/v1/client/session', {
      method: 'POST',
      body: { ticket: issued.ticket },
    });
    this.csrf = state.csrf_token || '';
    return state;
  }

  async logout() {
    await this.request('/api/v1/client/session', { method: 'DELETE' });
    this.csrf = '';
  }

  capabilities() {
    return this.request('/api/v1/capabilities');
  }

  /** Read-only local usage cards (messages, estimated tokens, voice, tools). */
  usageStats() {
    return this.request('/api/v1/usage/stats');
  }

  companions() {
    return this.request('/api/v1/companions');
  }

  conversations(companionId, state = 'active') {
    return this.request(
      '/api/v1/conversations?companion_id=' + encodeURIComponent(companionId) + '&state=' + encodeURIComponent(state),
    );
  }

  createConversation(companionId, title = '') {
    return this.request('/api/v1/conversations', {
      method: 'POST',
      body: { companion_id: companionId, title },
    });
  }

  updateConversation(conversationId, body) {
    return this.request('/api/v1/conversations/' + encodeURIComponent(conversationId), {
      method: 'PATCH',
      body,
    });
  }

  deleteConversation(conversationId) {
    return this.request('/api/v1/conversations/' + encodeURIComponent(conversationId), {
      method: 'DELETE',
    });
  }

  history(conversationId, limit = 200) {
    return this.request(
      '/api/v1/conversations/' + encodeURIComponent(conversationId) + '/events?limit=' + limit,
    );
  }

  event(eventId) {
    return this.request('/api/v1/events/' + encodeURIComponent(eventId));
  }

  persona(companionId) {
    return this.request('/api/v1/companions/' + encodeURIComponent(companionId) + '/persona');
  }

  patchPersona(companionId, expectedVersion, patch) {
    return this.request('/api/v1/companions/' + encodeURIComponent(companionId) + '/persona', {
      method: 'PATCH',
      body: { expected_version: expectedVersion, patch },
    });
  }

  memorySearch(companionId, conversationId, query) {
    return this.request('/api/v1/memory/search', {
      method: 'POST',
      body: { companion_id: companionId, conversation_id: conversationId, query },
    });
  }

  correction(memoryId, correctionText, sourceEventId) {
    return this.request('/api/v1/memory/corrections', {
      method: 'POST',
      body: { memory_id: memoryId, correction_text: correctionText, source_event_id: sourceEventId },
    });
  }

  memoryItems(companionId, { q = '', kind = '', status = '', sort = 'created', limit = 100,
                             pool = '', roomId = '', subjectPrincipalId = '' } = {}) {
    const params = new URLSearchParams({ companion_id: companionId, limit: String(limit) });
    if (q) params.set('q', q);
    if (kind) params.set('kind', kind);
    if (status) params.set('status', status);
    if (sort) params.set('sort', sort);
    if (pool) params.set('pool', pool);
    if (roomId) params.set('room_id', roomId);
    if (subjectPrincipalId) params.set('subject_principal_id', subjectPrincipalId);
    return this.request('/api/v1/memory/items?' + params.toString());
  }

  qqGroups(companionId) {
    return this.request('/api/v1/integrations/qq/groups?companion_id=' + encodeURIComponent(companionId));
  }

  memoryItem(memoryId) {
    return this.request('/api/v1/memory/items/' + encodeURIComponent(memoryId));
  }

  memorySetStatus(memoryId, status) {
    return this.request('/api/v1/memory/items/' + encodeURIComponent(memoryId), {
      method: 'PATCH',
      body: { status },
    });
  }

  memorySetImportance(memoryId, importance) {
    return this.request('/api/v1/memory/items/' + encodeURIComponent(memoryId), {
      method: 'PATCH',
      body: { importance },
    });
  }

  memoryUpdateItem(memoryId, body) {
    return this.request('/api/v1/memory/items/' + encodeURIComponent(memoryId), { method: 'PATCH', body });
  }

  memorySummary(companionId, level = 'daily') {
    return this.request('/api/v1/memory/consolidations', { method: 'POST', body: { companion_id: companionId, level } });
  }

  memoryRecallTest(companionId, query) {
    return this.request('/api/v1/memory/recall-test', {
      method: 'POST',
      body: { companion_id: companionId, query },
    });
  }

  memoryMaintenance(companionId) {
    return this.request('/api/v1/memory/maintenance', { method: 'POST', body: { companion_id: companionId } });
  }

  memoryStats(companionId) {
    return this.request('/api/v1/memory/stats?companion_id=' + encodeURIComponent(companionId));
  }

  memorySettings() {
    return this.request('/api/v1/memory/settings');
  }

  memoryUpdateSettings(body) {
    return this.request('/api/v1/memory/settings', { method: 'PATCH', body });
  }

  memoryJobs(companionId, limit = 20) {
    return this.request('/api/v1/memory/extractions?companion_id=' + encodeURIComponent(companionId) + '&limit=' + limit);
  }

  memoryExtractNow(companionId) {
    return this.request('/api/v1/memory/extractions', { method: 'POST', body: { companion_id: companionId } });
  }

  voiceSettings() {
    return this.request('/api/v1/voice/settings');
  }

  voiceUpdateSettings(body) {
    return this.request('/api/v1/voice/settings', { method: 'PATCH', body });
  }

  voices() {
    return this.request('/api/v1/voices');
  }

  ambientStatus() {
    return this.request('/api/v1/ambient/status');
  }

  ambientUpdateSettings(body) {
    return this.request('/api/v1/ambient/settings', { method: 'PATCH', body });
  }

  async ambientScreenshot(id) {
    const headers = { Accept: 'image/jpeg', ...this.authHeaders() };
    let response;
    try {
      response = await fetch(this.url('/api/v1/ambient/observations/' + encodeURIComponent(id) + '/screenshot'), {
        headers,
        credentials: this.bearer ? 'omit' : 'same-origin',
        cache: 'no-store',
      });
    } catch (error) {
      throw new ApiError('NETWORK', '无法连接服务：' + error.message, 0);
    }
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch {}
      const detail = payload && payload.error ? payload.error : null;
      throw new ApiError(detail ? detail.code : 'HTTP_' + response.status, detail ? detail.message : '读取截图失败', response.status);
    }
    return response.blob();
  }

  generationSpeech(generationId) {
    return this.request('/api/v1/generations/' + encodeURIComponent(generationId) + '/speech');
  }

  async voicePreview(body) {
    const headers = { Accept: 'audio/wav, application/json', ...this.authHeaders() };
    headers['Content-Type'] = 'application/json';
    if (!this.bearer && this.csrf) headers['X-AGI-CSRF'] = this.csrf;
    let response;
    try {
      response = await fetch(this.url('/api/v1/voice/preview'), {
        method: 'POST',
        headers,
        body: JSON.stringify(body || {}),
        credentials: this.bearer ? 'omit' : 'same-origin',
        cache: 'no-store',
      });
    } catch (error) {
      throw new ApiError('NETWORK', '无法连接语音服务：' + error.message, 0);
    }
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch {}
      const detail = payload && payload.error ? payload.error : null;
      throw new ApiError(detail ? detail.code : 'HTTP_' + response.status, detail ? detail.message : '语音试听失败', response.status);
    }
    return response.blob();
  }

  chat(payload) {
    return this.request('/api/v1/chat/requests', { method: 'POST', body: payload });
  }

  interrupt(generationId, commandId) {
    return this.request('/api/v1/generations/' + encodeURIComponent(generationId) + '/interrupt', {
      method: 'POST',
      body: { command_id: commandId },
    });
  }

  /**
   * Consume an SSE endpoint with fetch + ReadableStream so cookies/headers work.
   * Calls onEvent({id, type, data}) for every frame and resolves at end of stream.
   */
  async stream(path, { onEvent, signal, lastEventId } = {}) {
    const headers = { Accept: 'text/event-stream', ...this.authHeaders() };
    if (lastEventId) headers['Last-Event-ID'] = String(lastEventId);
    const response = await fetch(this.url(path), {
      headers,
      credentials: this.bearer ? 'omit' : 'same-origin',
      cache: 'no-store',
      signal,
    });
    if (!response.ok || !response.body) {
      throw new ApiError('STREAM_UNAVAILABLE', '事件流不可用', response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 2);
        if (!block || block.startsWith(':')) continue;
        const event = parseEventBlock(block);
        if (event && onEvent) onEvent(event);
      }
    }
  }
}

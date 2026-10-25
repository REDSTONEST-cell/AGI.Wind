/** Isolated Agent settings page. Credentials never enter localStorage or logs. */
const BOOLEAN_FIELDS = ['enabled', 'memory_search_enabled', 'sticker_search_enabled', 'sticker_send_enabled', 'web_search_enabled'];
const NUMBER_FIELDS = ['max_rounds', 'max_tool_calls', 'tool_timeout_seconds', 'total_timeout_seconds', 'max_result_chars', 'search_max_results'];

export function createAgentPage(client) {
  const form = document.querySelector('#agent-settings-form');
  const hint = document.querySelector('#agent-settings-hint');
  const status = document.querySelector('#agent-tools-status');
  let loading = false;
  let token = 0;
  installApprovals(client);
  function paint(data) {
    for (const field of BOOLEAN_FIELDS) form.elements[field].checked = data[field];
    for (const field of NUMBER_FIELDS) form.elements[field].value = data[field];
    form.elements.search_backend.value = data.search_backend;
    form.elements.search_url.value = data.search_url;
    form.elements.file_access_mode.value = data.file_access_mode || 'ask';
    document.querySelector('#agent-admin-state').textContent = data.administrator ? '服务当前以管理员身份运行' : '服务当前使用普通用户权限';
    document.querySelector('#agent-admin-request').disabled = Boolean(data.administrator);
    form.elements.api_key.value = '';
    form.elements.api_key.placeholder = data.search_key_inherited ? '正在复用 DeepSeek 模型密钥，无需填写' : data.search_key_configured ? '已保存，留空保留' : 'DeepSeek 自动复用已配置模型密钥；也可单独填写';
    form.elements.clear_key.checked = false;
    status.replaceChildren(...data.tools.map(tool => {
      const row = document.createElement('span');
      row.className = 'agent-tool-status';
      row.textContent = tool.label + ' · ' + (tool.available ? '可用' : '未启用或未配置');
      return row;
    }));
    document.querySelector('#agent-state').textContent = data.enabled ? '已开启 · 下一轮对话生效' : '已关闭 · 保持普通聊天';
    document.querySelector('#agent-search-state').textContent = data.web_search_enabled
      ? (data.search_ready ? '已配置检索服务' : '尚未配置可用检索服务')
      : '联网检索已关闭';
    document.querySelector('#agent-model-state').textContent = data.model_tools_available
      ? '使用当前对话模型；该模型需支持工具调用'
      : '请先配置支持工具调用的对话模型';
    backendVisibility();
  }
  function backendVisibility() {
    document.querySelector('#agent-search-url-field').hidden = form.elements.search_backend.value !== 'searxng';
  }
  async function load() {
    const version = ++token;
    hint.textContent = '正在读取…';
    try {
      const data = await client.request('/api/v1/agent/settings');
      if (version !== token) return;
      paint(data); hint.textContent = '';
    } catch (error) { if (version === token) hint.textContent = '读取失败：' + error.message; }
  }
  form.elements.search_backend.addEventListener('change', backendVisibility);
  document.querySelector('#agent-refresh').addEventListener('click', load);
  document.querySelector('#agent-admin-request').addEventListener('click', async () => {
    if (!window.confirm('将请求Windows管理员权限并重启服务。管理员权限会扩大文件工具可访问范围，仍需在系统UAC窗口确认。是否继续？')) return;
    try {
      const result = await client.request('/api/v1/agent/admin/request', { method: 'POST', body: { confirmed: true } });
      hint.textContent = result.message || '服务已是管理员身份';
    } catch (error) { hint.textContent = '请求失败：' + error.message; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (loading) return;
    loading = true;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    hint.textContent = '正在保存…';
    ++token;
    const payload = {};
    for (const field of BOOLEAN_FIELDS) payload[field] = form.elements[field].checked;
    for (const field of NUMBER_FIELDS) payload[field] = Number(form.elements[field].value);
    payload.search_backend = form.elements.search_backend.value;
    payload.search_url = form.elements.search_url.value.trim();
    payload.file_access_mode = form.elements.file_access_mode.value;
    const key = form.elements.api_key.value.trim();
    if (key) payload.api_key = key;
    payload.clear_key = form.elements.clear_key.checked;
    try {
      paint(await client.request('/api/v1/agent/settings', { method: 'PATCH', body: payload }));
      hint.textContent = '已保存';
    } catch (error) { hint.textContent = '保存失败：' + error.message; }
    finally { loading = false; button.disabled = false; }
  });
  return { load };
}

function installApprovals(client) {
  const dialog = document.querySelector('#agent-approval-dialog');
  let active = null, busy = false;
  const hint = document.querySelector('#agent-approval-hint');
  async function decide(allow) {
    if (!active || busy) return;
    busy = true;
    try {
      await client.request('/api/v1/agent/approvals/' + encodeURIComponent(active.id), { method: 'POST', body: { allow } });
      active = null; dialog.close();
    } catch (error) { hint.textContent = error.message; }
    finally { busy = false; }
  }
  document.querySelector('#agent-approval-allow').addEventListener('click', () => decide(true));
  document.querySelector('#agent-approval-deny').addEventListener('click', () => decide(false));
  dialog.addEventListener('cancel', event => { event.preventDefault(); decide(false); });
  setInterval(async () => {
    if (busy || document.hidden || document.querySelector('#app').hidden) return;
    busy = true;
    try {
      const result = await client.request('/api/v1/agent/approvals');
      if (active && !result.items.some(item => item.id === active.id)) { active = null; dialog.close(); }
      if (!active && result.items.length) {
        active = result.items[0]; hint.textContent = '';
        document.querySelector('#agent-approval-details').textContent = active.operation + '\n' + JSON.stringify(active.details, null, 2);
        dialog.showModal();
      }
    } catch { /* A disconnected or signed-out client cannot approve operations. */ }
    finally { busy = false; }
  }, 1500);
}

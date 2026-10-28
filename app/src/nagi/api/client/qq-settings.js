/** Owner-only OneBot settings. No QQ passwords or browser-persisted tokens. */
export function createQQPage(client, getConversation, openWindow = null) {
  const $ = selector => document.querySelector(selector);
  let policy = null;
  let groups = [];
  const labels = {disabled:'未启用',waiting:'等待 QQ 接入端连接',ready:'已连接 · 可以私聊和群聊',
    generating:'正在回复',debouncing:'正在等你把话说完',stopped:'已停止',failed:'操作失败，请检查系统日志',
    queue_full:'消息较多，请稍候',generation_stopped:'回答已停止',
    group_observed:'群消息已归档 · 未触发回复',group_ignored:'该群未启用接入',
    group_duplicate:'重复上报已忽略'};

  function groupIds(value) {
    return [...new Set(String(value || '').split(/[\s,，;；]+/).map(item => item.trim()).filter(Boolean))];
  }

  function show(value, fill = true) {
    policy = value;
    $('#qq-status').textContent = value.connected ? (labels[value.status] || '已连接') : (value.enabled ? '等待 QQ 接入端连接' : '未启用');
    const url = new URL(client.url(value.ws_path), location.origin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    $('#qq-ws-address').value = url.href;
    $('#qq-key-status').textContent = value.token_configured ? '连接令牌已保存；留空保持原值' : '尚未设置连接令牌';
    if (!fill) return;
    $('#qq-enabled').checked = value.enabled;
    $('#qq-owner').value = value.owner_qq || '';
    $('#qq-bot').value = value.bot_qq || '';
    $('#qq-delay').value = value.reply_delay_seconds;
    $('#qq-min-gap').value = value.reply_min_gap_seconds ?? 1.8;
    $('#qq-max-delay').value = value.reply_max_delay_seconds ?? 8;
    $('#qq-max-segments').value = value.reply_max_segments ?? 8;
    $('#qq-debounce-enabled').checked = value.debounce_enabled !== false;
    $('#qq-debounce-quiet').value = value.debounce_quiet_seconds ?? 2.2;
    $('#qq-debounce-max-wait').value = value.debounce_max_wait_seconds ?? 8;
    $('#qq-debounce-final-grace').value = value.debounce_final_grace_seconds ?? 0.9;
    $('#qq-group-enabled').checked = Boolean(value.group_enabled);
    $('#qq-allowed-groups').value = (value.allowed_groups || []).join(', ');
    $('#qq-group-trigger').value = value.group_trigger_mode || 'smart';
    $('#qq-followup-seconds').value = value.followup_seconds || 90;
    $('#qq-followup-rounds').value = value.followup_max_rounds || 4;
    $('#qq-group-context').value = value.group_context_messages || 100;
    $('#qq-conversation').textContent = value.conversation_id ? '已绑定对话；归档后需重新绑定' : '保存时绑定网页当前对话';
  }

  function renderGroups() {
    const wrap = $('#qq-group-windows');
    const rows = $('#qq-group-rows');
    if (!wrap || !rows) return;
    rows.replaceChildren();
    wrap.hidden = !groups.length;
    for (const group of groups) {
      const row = document.createElement('div');
      row.className = 'qq-group-row';
      const copy = document.createElement('div');
      copy.className = 'qq-group-copy';
      const title = document.createElement('strong');
      title.textContent = group.title || ('QQ群 ' + group.group_id);
      const meta = document.createElement('small');
      meta.textContent = '群号 ' + group.group_id + (group.enabled ? ' · 已启用' : ' · 已停用');
      copy.append(title, meta);
      const label = document.createElement('label');
      label.className = 'qq-group-switch';
      const toggle = document.createElement('input');
      toggle.type = 'checkbox';
      toggle.className = 'switch';
      toggle.checked = Boolean(group.enabled);
      toggle.setAttribute('aria-label', '启停 ' + (group.title || group.group_id));
      toggle.addEventListener('change', () => toggleGroup(group, toggle));
      label.append(toggle);
      row.append(copy, label);
      if (openWindow) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost small';
        button.textContent = '打开窗口';
        button.addEventListener('click', () => openWindow(group.conversation_id));
        row.append(button);
      }
      rows.append(row);
    }
  }

  async function loadGroups() {
    const companionId = getConversation()?.companion_id;
    if (!companionId) { groups = []; renderGroups(); return; }
    try { groups = await client.qqGroups(companionId); }
    catch { groups = []; }
    renderGroups();
  }

  async function toggleGroup(group, toggle) {
    const target = toggle.checked;
    toggle.disabled = true;
    try {
      const result = await client.request('/api/v1/integrations/qq/groups/' + encodeURIComponent(group.binding_id), {
        method: 'PATCH', body: {enabled: target},
      });
      group.enabled = Boolean(result.enabled);
      $('#qq-hint').textContent = (group.title || group.group_id) + (result.enabled ? ' 已启用。' : ' 已停用，不再触发回复和成员记忆。');
      show(await client.request('/api/v1/integrations/qq'));
      await loadGroups();
    } catch (error) {
      toggle.checked = !target;
      $('#qq-hint').textContent = '群启停失败：' + error.message;
    } finally {
      toggle.disabled = false;
    }
  }

  async function load(fill = true) {
    try {
      show(await client.request('/api/v1/integrations/qq'), fill);
      await loadGroups();
    }
    catch (error) { $('#qq-status').textContent = '读取失败：' + error.message; }
  }

  $('#qq-refresh').addEventListener('click', () => load(false));
  $('#qq-generate-token').addEventListener('click', () => {
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    $('#qq-token').value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    $('#qq-token').type = 'text';
    $('#qq-key-status').textContent = '请复制令牌到 SnowLuma，再保存设置';
  });
  $('#qq-settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#qq-save');
    button.disabled = true;
    try {
      const body = {enabled:$('#qq-enabled').checked, owner_qq:$('#qq-owner').value.trim(),
        bot_qq:$('#qq-bot').value.trim(), reply_delay_seconds:Number($('#qq-delay').value),
        reply_min_gap_seconds:Number($('#qq-min-gap').value),
        reply_max_delay_seconds:Number($('#qq-max-delay').value),
        reply_max_segments:Number($('#qq-max-segments').value),
        debounce_enabled:$('#qq-debounce-enabled').checked,
        debounce_quiet_seconds:Number($('#qq-debounce-quiet').value),
        debounce_max_wait_seconds:Number($('#qq-debounce-max-wait').value),
        debounce_final_grace_seconds:Number($('#qq-debounce-final-grace').value),
        group_enabled:$('#qq-group-enabled').checked,
        allowed_groups:groupIds($('#qq-allowed-groups').value),
        group_trigger_mode:$('#qq-group-trigger').value,
        followup_seconds:Number($('#qq-followup-seconds').value),
        followup_max_rounds:Number($('#qq-followup-rounds').value),
        group_context_messages:Number($('#qq-group-context').value) || 100,
        conversation_id:getConversation()?.id || policy?.conversation_id || null};
      const token = $('#qq-token').value.trim();
      if (token) body.token = token;
      show(await client.request('/api/v1/integrations/qq', {method:'PUT', body}));
      $('#qq-token').value = ''; $('#qq-token').type = 'password';
      $('#qq-hint').textContent = body.group_enabled
        ? '已保存。只有填写过的群会接入；群成员使用独立成员记忆，不会写入主人池。'
        : '已保存。群聊保持关闭。';
      await loadGroups();
    } catch (error) { $('#qq-hint').textContent = '保存失败：' + error.message; }
    finally { button.disabled = false; }
  });
  return {load};
}

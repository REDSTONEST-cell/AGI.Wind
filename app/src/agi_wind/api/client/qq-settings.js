/** Owner-only OneBot settings. No QQ passwords or browser-persisted tokens. */
export function createQQPage(client, getConversation) {
  const $ = selector => document.querySelector(selector);
  let policy = null;
  const labels = {disabled:'未启用',waiting:'等待 QQ 接入端连接',ready:'已连接 · 可以私聊和群聊',
    generating:'正在回复',stopped:'已停止',failed:'操作失败，请检查系统日志',
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
    $('#qq-group-enabled').checked = Boolean(value.group_enabled);
    $('#qq-allowed-groups').value = (value.allowed_groups || []).join(', ');
    $('#qq-group-trigger').value = value.group_trigger_mode || 'smart';
    $('#qq-followup-seconds').value = value.followup_seconds || 90;
    $('#qq-followup-rounds').value = value.followup_max_rounds || 4;
    $('#qq-conversation').textContent = value.conversation_id ? '已绑定对话；归档后需重新绑定' : '保存时绑定网页当前对话';
  }

  async function load(fill = true) {
    try { show(await client.request('/api/v1/integrations/qq'), fill); }
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
        group_enabled:$('#qq-group-enabled').checked,
        allowed_groups:groupIds($('#qq-allowed-groups').value),
        group_trigger_mode:$('#qq-group-trigger').value,
        followup_seconds:Number($('#qq-followup-seconds').value),
        followup_max_rounds:Number($('#qq-followup-rounds').value),
        conversation_id:getConversation()?.id || policy?.conversation_id || null};
      const token = $('#qq-token').value.trim();
      if (token) body.token = token;
      show(await client.request('/api/v1/integrations/qq', {method:'PUT', body}));
      $('#qq-token').value = ''; $('#qq-token').type = 'password';
      $('#qq-hint').textContent = body.group_enabled
        ? '已保存。只有填写过的群会接入；群成员使用独立成员记忆，不会写入主人池。'
        : '已保存。群聊保持关闭。';
    } catch (error) { $('#qq-hint').textContent = '保存失败：' + error.message; }
    finally { button.disabled = false; }
  });
  return {load};
}

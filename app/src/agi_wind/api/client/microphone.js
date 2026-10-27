const STORAGE_KEY = 'agiwind-microphone';
export function microphoneConstraints(devices = [], selected = '') {
  const inputs = devices.filter(d => d.kind === 'audioinput');
  const id = inputs.find(d => d.deviceId === selected)?.deviceId
    || inputs.find(d => d.deviceId === 'default')?.deviceId;
  return {audio:{channelCount:1, echoCancellation:true, noiseSuppression:true,
    autoGainControl:true, ...(id ? {deviceId:{exact:id}} : {})}, video:false};
}
export function microphoneError(error) {
  const messages = {
    NotAllowedError:'麦克风权限被拒绝，请在地址栏的网站权限中允许麦克风，并检查系统隐私设置',
    NotFoundError:'没有找到麦克风，请连接设备，或在麦克风设置中选择实体麦克风',
    NotReadableError:'麦克风无法打开，可能被其他程序占用或被系统禁用，请换一个输入设备',
    OverconstrainedError:'所选麦克风已不可用，请重新检测并选择麦克风',
    SecurityError:'浏览器禁止麦克风访问，请检查网站权限并使用 HTTPS 或本机地址',
  };
  return messages[error?.name] || error?.message || '麦克风连接失败';
}
export function bindMicrophoneSettings({error, busy = () => false}) {
  const select = document.querySelector('#microphone-device');
  const hint = document.querySelector('#microphone-status');
  const refresh = document.querySelector('#microphone-refresh');
  let selected = '';
  try { selected = localStorage.getItem(STORAGE_KEY) || ''; } catch {}
  function available() {
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia)
      throw new Error('麦克风需要安全连接：本机使用 localhost 或 127.0.0.1，手机请使用 HTTPS');
  }
  async function devices() {
    available();
    return navigator.mediaDevices.enumerateDevices();
  }
  async function render() {
    const rows = (await devices()).filter(d => d.kind === 'audioinput');
    select.replaceChildren();
    const option = document.createElement('option');
    option.value = ''; option.textContent = '系统默认麦克风'; select.append(option);
    rows.filter(d => !['default','communications'].includes(d.deviceId)).forEach((d, i) => {
      const option = document.createElement('option'); option.value = d.deviceId;
      option.textContent = d.label || `麦克风 ${i + 1}（检测后显示名称）`; select.append(option);
    });
    if (selected && !rows.some(d => d.deviceId === selected)) selected = '';
    select.value = selected;
    return rows;
  }
  async function open() {
    available();
    const stream = await navigator.mediaDevices.getUserMedia(microphoneConstraints(await devices(), selected));
    const track = stream.getAudioTracks()[0];
    hint.textContent = '当前输入：' + (track?.label || '已连接麦克风');
    void render().catch(() => {});
    return stream;
  }
  select.addEventListener('change', () => {
    selected = select.value;
    try { localStorage.setItem(STORAGE_KEY, selected); } catch {}
    hint.textContent = '将在下次录音或通话时使用所选麦克风';
  });
  refresh.addEventListener('click', async () => {
    if (busy()) { error('请先结束录音或通话，再检测麦克风'); return; }
    refresh.disabled = true;
    try { const stream = await open(); stream.getTracks().forEach(t => t.stop()); await render(); }
    catch (e) { hint.textContent = microphoneError(e); error(hint.textContent); }
    finally { refresh.disabled = false; }
  });
  navigator.mediaDevices?.addEventListener?.('devicechange', () => void render().catch(() => {}));
  void render().catch(e => { hint.textContent = microphoneError(e); });
  return {open};
}

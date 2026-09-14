const $ = id => document.getElementById(id);
const fmt = (value, digits = 1) => value == null ? '—' : Number(value).toFixed(digits);
const axes = [['throttle', '油门'], ['yaw', '偏航'], ['pitch', '俯仰'], ['roll', '横滚']];
const keys = new Set();
let desired = {throttle: .5, yaw: 0, pitch: 0, roll: 0};
let connected = false, initialized = false, source = 'synthetic';
let latencyHistory = [], fpsHistory = [], inputBusy = false;
let settingsVersion = 0, settingsPending = 0;
const settingTimers = {};

for (const [key, name] of axes) {
  const row = document.createElement('div'); row.className = 'axis';
  row.innerHTML = `<span>${name}</span><div class="axis-track"><i id="bar-${key}"></i></div><span class="axis-values"><b id="desired-${key}">—</b> / <span id="applied-${key}">—</span></span>`;
  $('axes').appendChild(row);
}

function error(message) { $('error').textContent = message; $('error').hidden = !message; }
async function post(path, value) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '设置失败');
  return data;
}
async function setting(value) {
  settingsVersion++; settingsPending++;
  try { await post('/api/link', value); error(''); }
  catch (e) { error(e.message); }
  finally { settingsPending--; }
}

function setLabels() {
  const kbps = Number($('capacity').value);
  $('capacityValue').textContent = kbps >= 1000 ? `${fmt(kbps / 1000)} Mbps` : `${kbps} kbps`;
  $('delayValue').textContent = `${$('delay').value} ms`;
  $('jitterValue').textContent = `${$('jitter').value} ms`;
  $('lossValue').textContent = `${$('loss').value}%`;
  $('lossBurstValue').textContent = Number($('lossBurst').value) <= 1 ? '独立丢包' : `平均连丢 ${$('lossBurst').value} 包`;
}
for (const [id, key, scale] of [['capacity', 'capacity_bps', 1000], ['delay', 'delay_ms', 1], ['jitter', 'jitter_ms', 1], ['loss', 'loss', .01], ['lossBurst', 'loss_burst', 1]]) {
  $(id).addEventListener('input', () => {
    setLabels(); settingsVersion++;
    clearTimeout(settingTimers[id]);
    settingTimers[id] = setTimeout(() => {
      delete settingTimers[id]; setting({[key]: Number($(id).value) * scale});
    }, 180);
  });
}
document.querySelectorAll('[data-kbps]').forEach(button => button.addEventListener('click', () => {
  clearTimeout(settingTimers.capacity); delete settingTimers.capacity;
  $('capacity').value = button.dataset.kbps; setLabels(); setting({capacity_bps: Number(button.dataset.kbps) * 1000});
}));
$('fusionMode').onclick = () => setting({mode: 'fusion'});
$('naiveMode').onclick = () => setting({mode: 'naive'});
for (const [id, key, on] of [['layeredOn', 'layered', true], ['layeredOff', 'layered', false], ['fecOn', 'fec', true], ['fecOff', 'fec', false]]) {
  $(id).onclick = async () => {
    try { await post('/api/video', {[key]: on}); error(''); }
    catch (e) { error(e.message); }
  };
}
const rungText = rung => `${rung[0]}×${rung[1]} q${rung[2]}`;
$('sourceSwitch').onclick = async () => {
  $('sourceSwitch').disabled = true;
  try { await post('/api/source', {source: source === 'synthetic' ? 'camera' : 'synthetic'}); error(''); }
  catch (e) { error(e.message); }
  finally { $('sourceSwitch').disabled = false; }
};

function neutral() { keys.clear(); desired = {throttle: .5, yaw: 0, pitch: 0, roll: 0}; sendInput(); paintKeys(); }
function keyDown(code) {
  if (!keys.has(code)) {
    if (code === 'KeyW') desired.throttle = Math.min(1, desired.throttle + .05);
    if (code === 'KeyS') desired.throttle = Math.max(0, desired.throttle - .05);
  }
  keys.add(code); paintKeys();
}
$('neutral').onclick = neutral;
const validKeys = new Set(['KeyW', 'KeyS', 'KeyA', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);
document.addEventListener('keydown', event => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
  if (event.code === 'Space' && event.target.tagName !== 'BUTTON') { event.preventDefault(); neutral(); }
  if (validKeys.has(event.code)) { event.preventDefault(); keyDown(event.code); }
});
document.addEventListener('keyup', event => { keys.delete(event.code); paintKeys(); });
window.addEventListener('blur', neutral);
document.addEventListener('visibilitychange', () => { if (document.hidden) neutral(); });
document.querySelectorAll('[data-key]').forEach(button => {
  button.addEventListener('pointerdown', event => { event.preventDefault(); button.setPointerCapture(event.pointerId); keyDown(button.dataset.key); });
  for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(event, () => { keys.delete(button.dataset.key); paintKeys(); });
});
function paintKeys() { document.querySelectorAll('[data-key]').forEach(b => b.classList.toggle('pressed', keys.has(b.dataset.key))); }
async function sendInput() {
  if (!connected || inputBusy || document.hidden) return;
  inputBusy = true;
  try { await post('/api/control', desired); }
  catch (_) { /* State polling reports connectivity; server watchdog returns to neutral. */ }
  finally { inputBusy = false; }
}
setInterval(() => {
  desired.throttle = Math.max(0, Math.min(1, desired.throttle + .05 * (Number(keys.has('KeyW')) - Number(keys.has('KeyS')))));
  desired.yaw = .6 * (Number(keys.has('KeyD')) - Number(keys.has('KeyA')));
  desired.pitch = .6 * (Number(keys.has('ArrowUp')) - Number(keys.has('ArrowDown')));
  desired.roll = .6 * (Number(keys.has('ArrowRight')) - Number(keys.has('ArrowLeft')));
  for (const [key] of axes) {
    $(`desired-${key}`).textContent = fmt(desired[key] * 100, 0) + '%';
    const bar = $(`bar-${key}`);
    bar.style.left = (key === 'throttle' ? 0 : Math.min(50, 50 + desired[key] * 50)) + '%';
    bar.style.width = (key === 'throttle' ? desired[key] * 100 : Math.abs(desired[key]) * 50) + '%';
  }
  sendInput();
}, 100);

function drawChart(id, data, color, floor) {
  const canvas = $(id), rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height, left = 35, top = 13, bottom = h - 10;
  const values = data.filter(v => v != null), max = Math.max(floor, ...values) * 1.15;
  ctx.font = '10px Segoe UI'; ctx.fillStyle = '#80929f'; ctx.strokeStyle = '#26313a'; ctx.lineWidth = .5;
  for (let i = 0; i <= 2; i++) {
    const y = top + (bottom - top) * i / 2;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.fillText(String(Math.round(max * (1 - i / 2))), 0, y + 3);
  }
  ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.beginPath(); let drawing = false;
  data.forEach((value, i) => {
    if (value == null) { drawing = false; return; }
    const x = left + (w - left) * (120 - data.length + i) / 119;
    const y = bottom - (bottom - top) * value / max;
    if (!drawing) ctx.moveTo(x, y); else ctx.lineTo(x, y); drawing = true;
  }); ctx.stroke();
}

async function poll() {
  const revision = settingsVersion;
  try {
    const response = await fetch('/api/state'); if (!response.ok) throw Error('服务未响应');
    const state = await response.json(); const m = state.metrics, c = state.config;
    connected = true; $('connectionDot').classList.add('online'); $('connectionText').textContent = '本机链路运行中';
    if (!initialized || (!settingsPending && !Object.keys(settingTimers).length && revision === settingsVersion)) {
      $('capacity').value = c.capacity_bps / 1000; $('delay').value = c.delay_ms;
      $('jitter').value = c.jitter_ms; $('loss').value = c.loss * 100; $('lossBurst').value = c.loss_burst;
      setLabels(); initialized = true;
    }
    for (const [id, mode] of [['fusionMode', 'fusion'], ['naiveMode', 'naive']]) {
      $(id).classList.toggle('selected', c.mode === mode); $(id).setAttribute('aria-pressed', String(c.mode === mode));
    }
    $('modeDescription').textContent = c.mode === 'fusion' ? '遥控最新值 → 遥测有界队列 → 视频帧队列' : '三类数据进入同一个 FIFO，依次等待发送';
    $('pathCapacity').textContent = fmt(c.capacity_bps / 1e6) + ' Mbps';
    const ctrl = m.latency_ms.CONTROL, data = m.latency_ms.DATA;
    $('controlP99').textContent = fmt(ctrl.p99); $('controlP50').textContent = fmt(ctrl.p50); $('controlP95').textContent = fmt(ctrl.p95);
    $('dataP95').textContent = fmt(data.p95); $('dataQueue').textContent = fmt(m.queue_ms.DATA.mean);
    $('videoFps').textContent = fmt(m.video_fps); $('frameDrops').textContent = m.counts.frames_dropped || 0;
    $('dropRate').textContent = fmt(m.frame_drop_rate * 100, 0) + '%';
    $('fullShare').textContent = m.video_full_share == null ? '—' : fmt(m.video_full_share * 100, 0) + '%';
    const v = state.video;
    for (const [id, value] of [['layeredOn', v.layered], ['layeredOff', !v.layered], ['fecOn', v.fec], ['fecOff', !v.fec]]) {
      $(id).classList.toggle('selected', value); $(id).setAttribute('aria-pressed', String(value));
    }
    const loss = v.loss_estimate == null ? null : fmt(v.loss_estimate * 100, 1) + '%';
    const recovered = m.counts.video_frames_recovered || 0;
    const burst = v.burst_estimate == null ? null : fmt(v.burst_estimate, 1);
    const depth = v.interleave_depth || 1;
    const interleave = depth > 1 ? ` · 实测突发 ${burst} 包，${depth} 帧交织` : '';
    $('fecDescription').textContent = !v.layered ? '单层模式不使用 FEC'
      : c.mode !== 'fusion' ? 'FIFO 模组不报告丢包率，不使用 FEC'
      : !v.fec ? `已关闭：实测丢包 ${loss}，高清帧任一分片丢失即整帧作废`
      : v.fec_parity ? `实测丢包 ${loss}${interleave} · 每帧加 ${v.fec_parity} 个冗余包（目标 99% 可还原）· 已救回 ${recovered} 帧`
      : `实测丢包 ${loss} · 暂不需要冗余包${recovered ? ` · 已救回 ${recovered} 帧` : ''}`;
    $('layerDescription').textContent = !v.layered ? `每帧一张 ${rungText(v.full_rung)}，拥塞时整帧丢弃`
      : c.mode !== 'fusion' ? `基础层 ${rungText(v.base_rung)} + 高清层固定 ${rungText(v.full_rung)}（FIFO 无容量预算）`
      : v.full_rung ? `基础层 ${rungText(v.base_rung)} 必发 · 高清层当前 ${rungText(v.full_rung)} · 基础层最多等高清 ${v.full_wait_ms} ms`
      : `基础层 ${rungText(v.base_rung)} 必发 · 高清层暂停（剩余容量装不下最低档）`;
    $('frameFormat').textContent = !v.frame_size ? '—'
      : `${v.frame_layer === 'base' ? '基础层' : v.layered ? '高清层' : ''} ${v.frame_size[0]} × ${v.frame_size[1]} / JPEG${v.frame_layer === 'base' ? ' · 浏览器放大' : ''}`.trim();
    $('wireRate').textContent = fmt(m.wire_mbps, 2); $('videoRate').textContent = fmt(m.video_mbps, 2);
    const t = state.telemetry;
    $('altitude').textContent = fmt(t.altitude); $('speed').textContent = fmt(t.speed); $('battery').textContent = fmt(t.battery); $('heading').textContent = fmt(t.yaw, 0);
    $('latitude').textContent = fmt(t.latitude, 6); $('longitude').textContent = fmt(t.longitude, 6);
    $('frameAge').textContent = fmt(state.frame_age_ms, 0); $('telemetryAge').textContent = fmt(state.telemetry_age_ms, 0);
    const stale = state.telemetry_age_ms == null || state.telemetry_age_ms > 500 || t.failsafe;
    $('telemetryStatus').textContent = !Object.keys(t).length ? '等待数据' : stale ? '状态过期 / 模拟归中' : '正在更新';
    $('telemetryStatus').classList.toggle('stale', !!stale);
    for (const [key] of axes) $(`applied-${key}`).textContent = t.applied_control ? fmt(t.applied_control[key] * 100, 0) + '%' : '—';
    source = state.source_requested;
    $('sourceSwitch').textContent = source === 'synthetic' ? '开启摄像头' : '使用合成画面';
    $('sourceLabel').textContent = state.source.startsWith('Camera') ? '电脑摄像头' : '合成测试画面';
    $('sourceWarning').textContent = state.source_warning ? '摄像头不可用，已回退到合成画面。' : '画面来自地面端完成重组的 JPEG；画面年龄包含排队与链路传输时间。';
    $('frameLabel').textContent = 'FRAME ' + String(state.frame_id).padStart(6, '0'); $('videoEmpty').hidden = state.frame_id > 0;
    const q = state.queues;
    $('queueLabel').textContent = c.mode === 'fusion' ? `C ${q.control} · D ${q.data} · B ${q.base_frames} · V ${q.video_frames} 帧` : `${q.fifo_packets} 包 · ${fmt(q.fifo_bytes / 1024, 0)} KB`;
    $('packetCount').textContent = `已接收 ${(m.counts.received || 0).toLocaleString()} 个数据包`;
    latencyHistory.push(ctrl.p99); fpsHistory.push(m.video_fps);
  } catch (e) {
    connected = false; $('connectionDot').classList.remove('online'); $('connectionText').textContent = '连接断开，正在重试';
    latencyHistory.push(null); fpsHistory.push(null);
  }
  latencyHistory = latencyHistory.slice(-120); fpsHistory = fpsHistory.slice(-120);
  drawChart('latencyChart', latencyHistory, '#5be0bd', 50); drawChart('fpsChart', fpsHistory, '#f1b663', 15);
  setTimeout(poll, 500);
}
poll();

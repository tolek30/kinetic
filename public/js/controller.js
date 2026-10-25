import { Rig } from './rig.js';
import { TalkInput } from './talk-input.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const STAGE = { width: 1920, height: 1080 };
const LAYERS = ['body', 'head'];
const LABELS = { body: 'Body', head: 'Head' };
const ROOM_ID = new URLSearchParams(location.search).get('instanceId') || 'host';
const ROOM_QUERY = `instanceId=${encodeURIComponent(ROOM_ID)}`;
const CLIENT_ID = (() => {
  try {
    let id = localStorage.getItem('kinetic.clientId');
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem('kinetic.clientId', id);
    }
    return id;
  } catch {
    return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
})();

function makeLayer(kind) {
  return {
    src: null,
    x: kind === 'body' ? 960 : 760,
    y: kind === 'body' ? 1080 : 700,
    scale: 1,
    rotation: 0,
    opacity: 1,
    visible: true,
    naturalWidth: null,
    naturalHeight: null
  };
}

function defaultConfig() {
  return {
    version: 3,
    stage: { ...STAGE },
    assets: { body: makeLayer('body'), head: makeLayer('head') },
    talk: { enabled: true, swing: 18, jitter: 0.72, sensitivity: 3.2, threshold: 0.05, smoothing: 0.1 },
    overlay: { background: 'transparent' }
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

let config = defaultConfig();
let revision = 0;
let selectedLayer = 'body';
let socket = null;
let socketOpen = false;
let reconnectTimer = null;
let reconnectDelay = 600;
let saveTimer = null;
let toastTimer = null;
let pendingUploadSlot = 'body';
let overlayCount = 0;
let networkOverlayUrl = '';
let appRole = 'host';
let hostAdminMode = false;
let hostApiVersion = 0;
let availableMicDevices = [];
let talkState = { talk: 0, level: 0, audio: false };
let testTalk = 0;
let talkTestFrame = 0;
let dragDepth = 0;
let booted = false;

const rig = new Rig($('#preview'), {
  editable: true,
  onSelect: selectLayer,
  onLayerChange: () => {
    syncInspector();
    markDirty();
  },
  onImageSize: (kind, width, height) => {
    config.assets[kind].naturalWidth = width;
    config.assets[kind].naturalHeight = height;
    syncAssets();
    markDirty();
  }
});

const talkInput = new TalkInput({
  onSample: handleTalkSample,
  onStatus: renderMicStatus
});
try {
  talkInput.deviceId = localStorage.getItem('kinetic.micDevice') || '';
} catch {
  talkInput.deviceId = '';
}

function setPill(node, state, label) {
  node.dataset.state = state;
  if (label) node.querySelector('span:last-child').textContent = label;
}

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('is-error', error);
  node.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('is-visible'), 2500);
}

function applyConfig(next, sync = true) {
  if (!next) return;
  config = next;
  rig.setConfig(config);
  talkInput.setConfig(config.talk);
  if (sync) syncAll();
}

async function loadConfig() {
  try {
    const response = await fetch(`/api/config?${ROOM_QUERY}`, { cache: 'no-store' });
    if (!response.ok) throw new Error('Server unavailable');
    const data = await response.json();
    revision = data.revision || 0;
    applyConfig(data.config);
    setPill($('#statusServer'), 'on');
  } catch {
    applyConfig(defaultConfig());
    setPill($('#statusServer'), 'error');
    toast('Could not connect to the local server.', true);
  }
  booted = true;
}

function syncAll() {
  syncAssets();
  syncInspector();
  syncTalkControls();
  syncOverlayControls();
  updateEmptyState();
}

function markDirty() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveConfig, 320);
}

async function saveConfig() {
  saveTimer = null;
  if (!config) return;
  const payload = clone(config);
  if (socketOpen) {
    socket.send(JSON.stringify({ type: 'config', config: payload }));
    return;
  }
  try {
    const response = await fetch(`/api/config?${ROOM_QUERY}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID },
      body: JSON.stringify({ config: payload })
    });
    if (!response.ok) throw new Error('Save failed');
    const data = await response.json();
    revision = data.revision || revision;
    setPill($('#statusServer'), 'on');
  } catch {
    setPill($('#statusServer'), 'error');
    toast('Changes could not be saved.', true);
  }
}

function connectSocket() {
  if (socket && socket.readyState <= WebSocket.OPEN) return;
  clearTimeout(reconnectTimer);
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${location.host}/ws?${ROOM_QUERY}`);
  socket.addEventListener('open', () => {
    socketOpen = true;
    reconnectDelay = 600;
    setPill($('#statusServer'), 'on');
    socket.send(JSON.stringify({ type: 'hello', role: 'controller', clientId: CLIENT_ID, instanceId: ROOM_ID }));
    sendMicDevices();
  });
  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'snapshot') {
      if (message.config && (!booted || message.revision > revision)) {
        revision = message.revision || revision;
        applyConfig(message.config);
      }
      if (message.clients) updateClients(message.clients);
      if (!hostAdminMode && typeof message.micDeviceId === 'string' && message.micDeviceId !== talkInput.deviceId) {
        chooseMicDevice(message.micDeviceId);
      }
    } else if (message.type === 'config' && message.config) {
      revision = message.revision || revision;
      applyConfig(message.config);
    } else if (message.type === 'saved') {
      revision = message.revision || revision;
    } else if (message.type === 'clients') {
      updateClients(message);
    } else if (message.type === 'mic-select' && !hostAdminMode) {
      chooseMicDevice(message.deviceId || '');
    }
  });
  socket.addEventListener('close', () => {
    socketOpen = false;
    setPill($('#statusServer'), 'warn');
    reconnectTimer = setTimeout(connectSocket, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.7, 8000);
  });
  socket.addEventListener('error', () => socket?.close());
}

function sendState(state) {
  talkState = state;
  rig.updateTalk(state);
  $('#audioBar').style.width = `${Math.round(clamp(state.level, 0, 1) * 100)}%`;
  $('#readoutTalk').textContent = `${Math.round(state.talk * 100)}%`;
  $('#readoutLift').textContent = `${(state.talk * config.talk.swing).toFixed(1)}°`;
  $('#previewMode').textContent = state.talk > 0.08 ? 'Head swinging with your voice' : state.audio ? 'Listening for speech' : 'Waiting for microphone';
  if (socketOpen) socket.send(JSON.stringify({ type: 'state', state }));
}

function handleTalkSample(state) {
  if (talkTestFrame) state = { ...state, talk: Math.max(state.talk, testTalk) };
  sendState(state);
}

function renderMicStatus(status) {
  const active = Boolean(status.audio);
  setPill($('#statusMic'), status.error ? 'error' : active ? 'on' : 'idle', status.error ? 'Mic error' : active ? 'Mic on' : 'Mic off');
  $('#audioStart').hidden = active;
  $('#audioStop').hidden = !active;
  if (hostAdminMode) {
    $('#audioStart').hidden = true;
    $('#audioStop').hidden = true;
  }
  $('#talkBadge').textContent = status.error ? 'Error' : active ? 'Listening' : 'Off';
  $('#micMessage').textContent = status.error || (active ? 'Listening locally for speech' : 'Enable mic to list available devices');
  if (status.error) toast(status.error, true);
}

function updateClients(clients) {
  overlayCount = clients.overlays || 0;
  setPill($('#statusOverlay'), overlayCount ? 'on' : 'idle', overlayCount ? `OBS · ${overlayCount}` : 'OBS');
  $('#obsConnected').textContent = `${overlayCount} live`;
  $('#obsConnected').classList.toggle('lime', overlayCount > 0);
}

function syncAssets() {
  let loaded = 0;
  for (const kind of LAYERS) {
    const layer = config.assets[kind];
    const card = $(`[data-card="${kind}"]`);
    const thumb = $(`#thumb${capitalize(kind)}`);
    const hasAsset = Boolean(layer?.src);
    if (hasAsset) loaded += 1;
    card.classList.toggle('is-loaded', hasAsset);
    card.classList.toggle('is-selected', selectedLayer === kind);
    thumb.classList.toggle('has-asset', hasAsset);
    thumb.style.backgroundImage = hasAsset ? `url("${layer.src}")` : '';
    $(`[data-remove="${kind}"]`).hidden = !hasAsset;
    $(`#meta${capitalize(kind)}`).textContent = !hasAsset
      ? 'Not loaded'
      : layer.naturalWidth && layer.naturalHeight
        ? `${layer.naturalWidth} × ${layer.naturalHeight} px`
        : 'Loaded';
  }
  $('#assetCount').textContent = `${loaded} / 2`;
  updateEmptyState();
}

function updateEmptyState() {
  const hasAsset = LAYERS.some((kind) => config.assets[kind].src);
  $('#emptyState').classList.toggle('is-hidden', hasAsset);
}

function selectLayer(kind) {
  if (!kind || !config.assets[kind]) return;
  selectedLayer = kind;
  rig.setSelected(kind);
  $$('[data-select]').forEach((button) => button.classList.toggle('is-active', button.dataset.select === kind));
  $$('.asset-card').forEach((card) => card.classList.toggle('is-selected', card.dataset.card === kind));
  $('#layerName').textContent = LABELS[kind];
  syncInspector();
}

function syncInspector() {
  const layer = config.assets[selectedLayer];
  for (const [id, value] of [['layerX', layer.x], ['layerY', layer.y], ['layerScale', layer.scale], ['layerRotation', layer.rotation], ['layerOpacity', layer.opacity]]) {
    const input = $(`#${id}`);
    input.value = value;
    updateRangeFill(input);
  }
  $('#outLayerX').textContent = `${Math.round(layer.x)} px`;
  $('#outLayerY').textContent = `${Math.round(layer.y)} px`;
  $('#outLayerScale').textContent = `${Math.round(layer.scale * 100)}%`;
  $('#outLayerRotation').textContent = `${Number(layer.rotation).toFixed(1)}°`;
  $('#outLayerOpacity').textContent = `${Math.round(layer.opacity * 100)}%`;
  $('#layerVisible').checked = layer.visible;
  $('#layerRemove').disabled = !layer.src;
  $('#layerFit').disabled = !layer.src;
  $('#headSit').hidden = selectedLayer !== 'head';
}

function setLayerValue(key, value) {
  config.assets[selectedLayer][key] = value;
  rig.setConfig(config);
  syncInspector();
  syncAssets();
  markDirty();
}

function autoFitLayer(layer, kind, width, height) {
  const scale = kind === 'body'
    ? Math.min((STAGE.width * 0.9) / width, (STAGE.height * 0.9) / height)
    : Math.min((STAGE.width * 0.38) / width, (STAGE.height * 0.46) / height);
  layer.scale = clamp(Number(scale.toFixed(4)), 0.02, 4);
  layer.x = kind === 'body' ? STAGE.width / 2 : STAGE.width / 2 - width * layer.scale / 2;
  layer.y = kind === 'body' ? STAGE.height : 700;
  layer.rotation = 0;
  layer.opacity = 1;
  layer.visible = true;
}

function sitHeadOnBody() {
  const body = config.assets.body;
  const head = config.assets.head;
  if (!body.src || !head.src || !body.naturalHeight || !head.naturalWidth) {
    toast('Load both body and head first.', true);
    return;
  }
  const bodyTop = body.y - body.naturalHeight * body.scale;
  head.x = body.x - head.naturalWidth * head.scale / 2;
  head.y = bodyTop;
  rig.setConfig(config);
  syncInspector();
  markDirty();
}

function fitSelectedLayer() {
  const layer = config.assets[selectedLayer];
  if (!layer.src) return;
  autoFitLayer(layer, selectedLayer, layer.naturalWidth || 500, layer.naturalHeight || 500);
  if (selectedLayer === 'head' && config.assets.body.src) {
    sitHeadOnBody();
    return;
  }
  rig.setConfig(config);
  syncInspector();
  markDirty();
}

function centerSelectedLayer() {
  const layer = config.assets[selectedLayer];
  if (selectedLayer === 'head' && config.assets.body.src) {
    sitHeadOnBody();
    return;
  }
  layer.x = selectedLayer === 'body' ? STAGE.width / 2 : STAGE.width / 2 - (layer.naturalWidth || 500) * layer.scale / 2;
  layer.y = selectedLayer === 'body' ? STAGE.height : 700;
  rig.setConfig(config);
  syncInspector();
  markDirty();
}

function removeLayer(kind) {
  const layer = config.assets[kind];
  if (!layer.src) return;
  Object.assign(layer, makeLayer(kind));
  rig.setConfig(config);
  syncAll();
  markDirty();
  toast(`${LABELS[kind]} removed.`);
}

async function uploadFile(file, slot) {
  if (!file) return;
  if (!file.type.startsWith('image/')) return toast('Choose an image file.', true);
  if (file.size > 30 * 1024 * 1024) return toast('Images must be under 30 MB.', true);
  slot ||= guessSlot(file.name);
  const previous = config.assets[slot];
  const body = new FormData();
  body.append('file', file);
  toast(`Uploading ${LABELS[slot].toLowerCase()}…`);
  try {
    const response = await fetch(`/api/upload?slot=${slot}&${ROOM_QUERY}`, {
      method: 'POST',
      headers: { 'X-Client-Id': CLIENT_ID },
      body
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Upload failed.');
    const next = clone(config);
    const layer = next.assets[slot];
    layer.src = data.url;
    layer.naturalWidth = data.width;
    layer.naturalHeight = data.height;
    if (previous.src && previous.naturalWidth) {
      layer.scale = clamp(previous.scale * previous.naturalWidth / data.width, 0.02, 4);
      layer.x = previous.x;
      layer.y = previous.y;
      layer.rotation = previous.rotation;
      layer.opacity = previous.opacity;
      layer.visible = previous.visible;
    } else {
      autoFitLayer(layer, slot, data.width, data.height);
    }
    revision = data.revision || revision;
    applyConfig(next);
    selectLayer(slot);
    if (slot === 'head' && config.assets.body.src && !previous.src) sitHeadOnBody();
    if (slot === 'body' && config.assets.head.src && !previous.src) sitHeadOnBody();
    markDirty();
    toast(`${LABELS[slot]} loaded.`);
  } catch (error) {
    toast(error.message || 'The image could not be uploaded.', true);
  }
}

function guessSlot(filename) {
  const name = filename.toLowerCase();
  if (name.includes('head')) return 'head';
  if (name.includes('body') || name.includes('torso')) return 'body';
  return LAYERS.find((kind) => !config.assets[kind].src) || 'body';
}

function syncTalkControls() {
  for (const input of $$('[data-setting]')) {
    const key = input.dataset.setting;
    input.value = config.talk[key];
    updateRangeFill(input);
    $(`#out${capitalize(key)}`).textContent = key === 'swing'
      ? `${Number(config.talk[key]).toFixed(1)}°`
      : `${Math.round(config.talk[key] * 100)}%`;
  }
}

function syncOverlayControls() {
  const localUrl = `${location.origin}/overlay/${encodeURIComponent(ROOM_ID)}`;
  const url = appRole === 'instance' ? networkOverlayUrl || localUrl : localUrl;
  $('#overlayUrl').value = url;
  $('#networkOverlayUrl').value = networkOverlayUrl || 'No LAN address detected';
  $('#copyNetworkOverlay').disabled = !networkOverlayUrl;
  $('#openOverlay').href = url;
  $('#openOverlayTop').href = url;
  $$('.bg-swatch').forEach((swatch) => swatch.classList.toggle('is-active', swatch.dataset.bg === config.overlay.background));
}

async function loadNetworkInfo() {
  try {
    const response = await fetch(`/api/health?${ROOM_QUERY}`, { cache: 'no-store' });
    const data = await response.json();
    appRole = data.role || 'host';
    hostApiVersion = Number(data.version) || 0;
    hostAdminMode = appRole === 'host' && ROOM_ID !== 'host';
    networkOverlayUrl = data.publicOverlayUrl || data.networkUrls?.[0] || data.overlayUrl || '';
    $('#hostDashboardLink').hidden = appRole !== 'host' || hostApiVersion < 4;
    $('#micDevice').hidden = hostAdminMode;
    $('#audioStart').hidden = hostAdminMode || talkInput.active;
    $('#audioStop').hidden = hostAdminMode || !talkInput.active;
    $('#audioMeter').hidden = hostAdminMode;
    $('#micMessage').hidden = hostAdminMode;
    $('#remoteMicNote').hidden = !hostAdminMode;
  } catch {
    networkOverlayUrl = '';
  }
  syncOverlayControls();
}

function handleSettingChange(input) {
  const key = input.dataset.setting;
  config.talk[key] = Number(input.value);
  talkInput.setConfig(config.talk);
  updateRangeFill(input);
  syncTalkControls();
  rig.setConfig(config);
  markDirty();
}

async function startAudio() {
  $('#audioStart').disabled = true;
  try {
    await talkInput.start();
    try { localStorage.setItem('kinetic.micDevice', talkInput.deviceId); } catch { /* storage may be disabled */ }
    await refreshMicDevices();
    toast('Microphone enabled.');
  } catch (error) {
    renderMicStatus(talkInput.status());
    toast(error.message || 'Microphone could not start.', true);
  } finally {
    $('#audioStart').disabled = false;
  }
}

async function refreshMicDevices() {
  if (hostAdminMode) return;
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const devices = (await navigator.mediaDevices.enumerateDevices())
      .filter((device) => device.kind === 'audioinput' && device.deviceId && device.deviceId !== 'default');
    const select = $('#micDevice');
    const selected = talkInput.deviceId;
    const canValidateSelection = devices.some((device) => device.label);
    select.replaceChildren(new Option('System default', ''));
    devices.forEach((device, index) => {
      select.add(new Option(device.label || `Microphone ${index + 1}`, device.deviceId));
    });
    availableMicDevices = devices.map((device) => ({
      deviceId: device.deviceId,
      label: device.label || 'Microphone'
    }));
    if (selected && !devices.some((device) => device.deviceId === selected) && canValidateSelection) {
      select.value = '';
      try { localStorage.removeItem('kinetic.micDevice'); } catch { /* storage may be disabled */ }
      if (talkInput.active) {
        try { await talkInput.selectDevice(''); } catch (error) { toast(error.message, true); }
      } else {
        talkInput.deviceId = '';
      }
    } else {
      select.value = selected;
      if (selected && select.value !== selected) select.value = '';
    }
    select.disabled = devices.length === 0;
    sendMicDevices();
  } catch {
    $('#micDevice').disabled = false;
  }
}

function sendMicDevices() {
  if (!socketOpen || hostAdminMode) return;
  socket.send(JSON.stringify({
    type: 'mic-devices',
    devices: availableMicDevices,
    selectedDeviceId: talkInput.deviceId
  }));
}

async function chooseMicDevice(deviceId) {
  try {
    localStorage.setItem('kinetic.micDevice', deviceId);
  } catch {
    /* device selection still works for this session */
  }
  $('#micDevice').disabled = true;
  try {
    await talkInput.selectDevice(deviceId);
    toast(talkInput.active ? 'Microphone switched.' : 'Microphone selected.');
  } catch (error) {
    toast(error.message || 'That microphone could not be started.', true);
  } finally {
    await refreshMicDevices();
  }
}

function testMotion() {
  cancelAnimationFrame(talkTestFrame);
  const started = performance.now();
  const pulse = (time) => {
    const progress = (time - started) / 2300;
    if (progress >= 1) {
      talkTestFrame = 0;
      testTalk = 0;
      sendState({ talk: 0, level: 0, audio: talkInput.active });
      return;
    }
    const envelope = Math.pow(Math.max(0, Math.sin(progress * Math.PI * 2.4)), 0.8) * 0.82;
    testTalk = envelope;
    const sample = { talk: envelope, level: envelope, audio: talkInput.active };
    talkTestFrame = requestAnimationFrame(pulse);
    sendState(sample);
  };
  talkTestFrame = requestAnimationFrame(pulse);
}

function syncLayerReadouts() {
  const layer = config.assets[selectedLayer];
  $('#outLayerX').textContent = `${Math.round(layer.x)} px`;
  $('#outLayerY').textContent = `${Math.round(layer.y)} px`;
  $('#outLayerScale').textContent = `${Math.round(layer.scale * 100)}%`;
  $('#outLayerRotation').textContent = `${Number(layer.rotation).toFixed(1)}°`;
  $('#outLayerOpacity').textContent = `${Math.round(layer.opacity * 100)}%`;
}

function updateRangeFill(input) {
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  input.style.setProperty('--fill', `${clamp((Number(input.value) - min) / (max - min) * 100, 0, 100)}%`);
}

function capitalize(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

async function copyOverlayUrl() {
  try {
    await navigator.clipboard.writeText($('#overlayUrl').value);
  } catch {
    const input = $('#overlayUrl');
    input.select();
    document.execCommand('copy');
  }
  toast('OBS link copied.');
}

async function copyNetworkOverlayUrl() {
  if (!networkOverlayUrl) return;
  try {
    await navigator.clipboard.writeText(networkOverlayUrl);
  } catch {
    const input = $('#networkOverlayUrl');
    input.select();
    document.execCommand('copy');
  }
  toast('Network overlay link copied.');
}

async function resetRig() {
  if (!confirm('Clear both layers and reset talk motion?')) return;
  clearTimeout(saveTimer);
  saveTimer = null;
  try {
    const response = await fetch(`/api/config/reset?${ROOM_QUERY}`, { method: 'POST', headers: { 'X-Client-Id': CLIENT_ID } });
    const data = await response.json();
    if (!response.ok) throw new Error();
    revision = data.revision || revision;
    applyConfig(data.config);
    toast('Rig reset.');
  } catch {
    toast('The rig could not be reset.', true);
  }
}

function bindEvents() {
  $$('[data-upload-slot]').forEach((button) => button.addEventListener('click', () => {
    pendingUploadSlot = button.dataset.uploadSlot;
    $('#fileInput').value = '';
    $('#fileInput').click();
  }));
  $('#fileInput').addEventListener('change', (event) => uploadFile(event.target.files?.[0], pendingUploadSlot));
  $$('[data-remove]').forEach((button) => button.addEventListener('click', () => removeLayer(button.dataset.remove)));
  $$('[data-select]').forEach((button) => button.addEventListener('click', () => selectLayer(button.dataset.select)));
  $$('.tabs [data-tab]').forEach((button) => button.addEventListener('click', () => {
    $$('.tabs [data-tab]').forEach((tab) => {
      const active = tab === button;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    $$('.panel').forEach((panel) => panel.classList.toggle('is-active', panel.dataset.panel === button.dataset.tab));
  }));

  for (const [selector, key] of [['#layerX', 'x'], ['#layerY', 'y'], ['#layerScale', 'scale'], ['#layerRotation', 'rotation'], ['#layerOpacity', 'opacity']]) {
    $(selector).addEventListener('input', (event) => {
      setLayerValue(key, Number(event.target.value));
      syncLayerReadouts();
    });
  }
  $('#layerVisible').addEventListener('change', (event) => {
    config.assets[selectedLayer].visible = event.target.checked;
    rig.setConfig(config);
    syncAssets();
    markDirty();
  });
  $('#layerCenter').addEventListener('click', centerSelectedLayer);
  $('#layerFit').addEventListener('click', fitSelectedLayer);
  $('#layerRemove').addEventListener('click', () => removeLayer(selectedLayer));
  $('#headSit').addEventListener('click', sitHeadOnBody);
  $$('[data-setting]').forEach((input) => input.addEventListener('input', () => handleSettingChange(input)));

  $('#audioStart').addEventListener('click', startAudio);
  $('#audioStop').addEventListener('click', () => {
    talkInput.stop();
    toast('Microphone disabled.');
  });
  $('#micDevice').addEventListener('change', (event) => chooseMicDevice(event.target.value));
  $('#testTalk').addEventListener('click', testMotion);
  $('#copyOverlay').addEventListener('click', copyOverlayUrl);
  $('#copyNetworkOverlay').addEventListener('click', copyNetworkOverlayUrl);
  $('#topCopy').addEventListener('click', copyOverlayUrl);
  $$('.bg-swatch').forEach((button) => button.addEventListener('click', () => {
    config.overlay.background = button.dataset.bg;
    rig.setConfig(config);
    syncOverlayControls();
    markDirty();
  }));
  $('#resetConfig').addEventListener('click', resetRig);
  $('#toggleGrid').addEventListener('click', () => {
    const visible = !$('#stageShell').classList.toggle('no-grid');
    $('#toggleGrid').classList.toggle('is-active', visible);
    $('#toggleGrid').setAttribute('aria-pressed', String(visible));
  });

  const shell = $('#stageShell');
  shell.addEventListener('dragenter', (event) => { event.preventDefault(); dragDepth += 1; shell.classList.add('is-dragover'); });
  shell.addEventListener('dragover', (event) => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; });
  shell.addEventListener('dragleave', (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) shell.classList.remove('is-dragover');
  });
  shell.addEventListener('drop', (event) => {
    event.preventDefault();
    dragDepth = 0;
    shell.classList.remove('is-dragover');
    const file = [...(event.dataTransfer?.files || [])].find((item) => item.type.startsWith('image/'));
    if (file) uploadFile(file, guessSlot(file.name));
    else toast('Drop an image file.', true);
  });

  document.addEventListener('keydown', (event) => {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    const layer = config.assets[selectedLayer];
    if (!layer.src) return;
    const step = event.shiftKey ? 10 : 1;
    if (event.key === 'ArrowLeft') layer.x -= step;
    else if (event.key === 'ArrowRight') layer.x += step;
    else if (event.key === 'ArrowUp') layer.y -= step;
    else if (event.key === 'ArrowDown') layer.y += step;
    else return;
    event.preventDefault();
    rig.setConfig(config);
    syncInspector();
    markDirty();
  });

  window.addEventListener('pagehide', () => {
    talkInput.stop();
    if (!saveTimer) return;
    clearTimeout(saveTimer);
      fetch(`/api/config?${ROOM_QUERY}`, {
      method: 'PUT',
      keepalive: true,
      headers: { 'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID },
      body: JSON.stringify({ config })
    });
  });
}

async function bootstrap() {
  bindEvents();
  await loadConfig();
  await loadNetworkInfo();
  await refreshMicDevices();
  navigator.mediaDevices?.addEventListener?.('devicechange', refreshMicDevices);
  connectSocket();
  setInterval(() => {
    if (socketOpen) socket.send(JSON.stringify({ type: 'ping' }));
  }, 20_000);
}

bootstrap();

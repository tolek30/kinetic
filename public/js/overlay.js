import { Rig } from './rig.js';

const host = document.getElementById('overlay');
const rig = new Rig(host, { editable: false });
const pathParts = location.pathname.split('/').filter(Boolean);
const ROOM_ID = pathParts[0] === 'overlay' ? pathParts[1] || 'host' : 'host';
let socket = null;
let reconnectDelay = 600;
let lastFrame = 0;
let debug = new URLSearchParams(location.search).has('debug');

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${location.host}/ws?instanceId=${encodeURIComponent(ROOM_ID)}`);

  socket.addEventListener('open', () => {
    reconnectDelay = 600;
    socket.send(JSON.stringify({ type: 'hello', role: 'overlay', instanceId: ROOM_ID }));
    if (debug) console.info('[kinetic] overlay connected');
  });

  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === 'snapshot' || message.type === 'config') {
      if (message.config) rig.setConfig(message.config);
      if (message.roomSettings) applyHostSettings(message.roomSettings);
      else if (message.hostSettings) applyHostSettings(message.hostSettings);
    } else if (message.type === 'state') {
      rig.updateTalk(message.state);
      lastFrame = performance.now();
      if (debug && Math.random() < 0.02) {
        console.debug('[kinetic] state', message.state);
      }
    } else if (message.type === 'room-settings' || message.type === 'host-settings') {
      applyHostSettings(message);
    }
  });

  socket.addEventListener('close', () => {
    if (debug) console.warn('[kinetic] overlay disconnected, retrying');
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.7, 8000);
  });

  socket.addEventListener('error', () => {
    socket?.close();
  });
}

function applyHostSettings(settings) {
  host.classList.toggle('is-mirrored', Boolean(settings.mirrorOutput));
}

async function bootstrap() {
  try {
    const response = await fetch(`/api/config?instanceId=${encodeURIComponent(ROOM_ID)}`, { cache: 'no-store' });
    const data = await response.json();
    if (data.config) rig.setConfig(data.config);
    const healthResponse = await fetch(`/api/health?instanceId=${encodeURIComponent(ROOM_ID)}`, { cache: 'no-store' });
    if (healthResponse.ok) applyHostSettings(await healthResponse.json());
  } catch (error) {
    if (debug) console.error('[kinetic] could not load config', error);
  }
  connect();
  setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'ping' }));
    }
  }, 20_000);
}

bootstrap();

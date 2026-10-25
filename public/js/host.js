const $ = (selector) => document.querySelector(selector);
const roomsNode = $('#roomList');
let hostState = null;
let hostBase = '';
let detectedPublicBase = '';
try { detectedPublicBase = localStorage.getItem('kinetic.publicHostUrl') || ''; } catch { /* storage may be unavailable */ }
let hostApiVersion = 0;
let toastTimer = 0;

function notify(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('is-visible'), 2200);
}

function roomUrl(id, fallback) {
  return hostBase ? `${hostBase}/overlay/${encodeURIComponent(id)}` : fallback;
}

function renderRooms() {
  const rooms = hostState?.rooms || [];
  roomsNode.replaceChildren();
  $('#emptyRooms').hidden = rooms.length > 0;
  const capacity = hostState?.settings?.maxRooms || 10;
  const online = rooms.filter((room) => room.clients?.controllers > 0).length;
  $('#onlineCount').textContent = `${rooms.length} / ${capacity}`;
  $('#roomCount').textContent = `${online} live · ${rooms.length} / ${capacity}`;
  for (const room of rooms) {
    const card = document.createElement('article');
    card.className = 'room-card';
    const top = document.createElement('div');
    top.className = 'room-top';
    const status = document.createElement('span');
    status.className = `room-status${room.clients?.controllers ? ' is-live' : ''}`;
    const name = document.createElement('strong');
    name.className = 'room-name';
    name.textContent = room.name;
    const kind = document.createElement('span');
    kind.className = 'room-kind';
    kind.textContent = room.id === 'host' ? 'HOST CAM' : 'INSTANCE';
    top.append(status, name, kind);
    const info = document.createElement('p');
    info.className = 'room-info';
    info.textContent = room.clients?.controllers ? 'Instance connected · live state relaying' : 'Waiting for this Instance to connect';
    if (hostApiVersion >= 7) {
      const mirrorRow = document.createElement('label');
      mirrorRow.className = 'room-mirror';
      const mirrorText = document.createElement('span');
      mirrorText.textContent = 'Mirror this cam in OBS';
      const mirrorInput = document.createElement('input');
      mirrorInput.type = 'checkbox';
      mirrorInput.checked = Boolean(room.mirrorOutput);
      mirrorInput.addEventListener('change', async () => {
        mirrorInput.disabled = true;
        try {
          const response = await fetch(`/api/instances/${encodeURIComponent(room.id)}/mirror`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mirrorOutput: mirrorInput.checked })
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Mirror setting failed.');
          notify(`${room.name} OBS output ${result.mirrorOutput ? 'mirrored' : 'unmirrored'}.`);
        } catch (error) {
          mirrorInput.checked = !mirrorInput.checked;
          notify(error.message || 'Mirror setting failed.');
        } finally {
          mirrorInput.disabled = false;
        }
      });
      mirrorRow.append(mirrorText, mirrorInput);
      card.append(mirrorRow);
    }
    if (hostApiVersion >= 4) {
      const micRow = document.createElement('label');
      micRow.className = 'room-mic';
      const micTitle = document.createElement('span');
      micTitle.textContent = room.micDevices?.length ? 'Microphone input' : 'Enable mic on Instance to list devices';
      const micSelect = document.createElement('select');
      micSelect.add(new Option('System default', ''));
      for (const device of room.micDevices || []) {
        micSelect.add(new Option(device.label, device.deviceId));
      }
      micSelect.value = room.selectedMic || '';
      micSelect.addEventListener('change', async () => {
        micSelect.disabled = true;
        try {
          const response = await fetch(`/api/instances/${encodeURIComponent(room.id)}/microphone`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deviceId: micSelect.value })
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Microphone switch failed.');
          notify(`${room.name} microphone switched.`);
        } catch (error) {
          notify(error.message || 'Microphone switch failed.');
        } finally {
          micSelect.disabled = false;
        }
      });
      micRow.append(micTitle, micSelect);
      card.append(micRow);
    }
    const urlRow = document.createElement('div');
    urlRow.className = 'room-url';
    const input = document.createElement('input');
    input.readOnly = true;
    input.value = roomUrl(room.id, room.overlayUrl);
    input.title = input.value;
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.textContent = 'Copy';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(input.value); }
      catch { input.select(); document.execCommand('copy'); }
      notify(`${room.name} OBS link copied.`);
    });
    urlRow.append(input, copy);
    card.append(top, info, urlRow);
    if (room.id === 'host') {
      const controls = document.createElement('a');
      controls.className = 'room-controls';
      controls.href = '/?instanceId=host';
      controls.textContent = 'Edit Host assets & controls';
      card.append(controls);
    } else if (hostApiVersion >= 4) {
      const controls = document.createElement('a');
      controls.className = 'room-controls';
      controls.href = `/?instanceId=${encodeURIComponent(room.id)}`;
      controls.textContent = 'Edit this cam’s assets';
      card.append(controls);
      const remove = document.createElement('button');
      remove.className = 'remove-room';
      remove.type = 'button';
      remove.textContent = 'Remove Instance';
      remove.addEventListener('click', async () => {
        if (!confirm(`Remove ${room.name} and its saved art?`)) return;
        const response = await fetch(`/api/instances/${encodeURIComponent(room.id)}`, { method: 'DELETE' });
        const result = await response.json();
        if (!response.ok) return notify(result.error || 'Could not remove Instance.');
        notify(`${room.name} removed.`);
        await refresh();
      });
      card.append(remove);
    }
    roomsNode.append(card);
  }
}

async function refresh() {
  try {
    const [hostResponse, healthResponse] = await Promise.all([
      fetch('/api/host', { cache: 'no-store' }),
      fetch('/api/health', { cache: 'no-store' })
    ]);
    if (!hostResponse.ok) throw new Error('Host dashboard is local-only.');
    hostState = await hostResponse.json();
    const health = await healthResponse.json();
    hostApiVersion = Number(health.version) || 0;
    $('#upgradeNotice').hidden = hostApiVersion >= 7;
    if (hostApiVersion < 5) {
      $('#upgradeNotice').textContent = `Use Public IP for share links now. Restart the Host later to save that URL and enable per-cam mirroring.`;
    } else if (hostApiVersion < 6) {
      $('#upgradeNotice').textContent = 'Restart the Host to enable per-cam OBS mirroring. Existing rooms and files are kept.';
    } else if (hostApiVersion < 7) {
      $('#upgradeNotice').textContent = 'Restart the Host to replace the global mirror switch with per-cam mirror controls.';
    }
    $('#publicAddressSettings').hidden = hostApiVersion < 5;
    const primary = health.networkUrls?.[0] || '';
    hostBase = health.publicUrl || detectedPublicBase || primary.replace(/\/overlay\/[^/]+$/, '');
    $('#hostAddress').textContent = hostBase || 'No address detected';
    const editing = document.activeElement?.matches('input, select');
    if (!editing) {
      $('#heroName').textContent = `${hostState.settings.name} Host`;
      $('#settingName').value = hostState.settings.name;
      $('#settingCode').value = hostState.settings.accessCode;
      $('#settingPublicUrl').value = hostState.settings.publicUrl || '';
      $('#settingCapacity').value = String(hostState.settings.maxRooms);
      renderRooms();
    }
  } catch (error) {
    $('#settingsMessage').textContent = error.message || 'Host is unavailable.';
  }
}

$('#copyAddress').addEventListener('click', async () => {
  const text = $('#hostAddress').textContent;
  if (!text.startsWith('http')) return;
  try { await navigator.clipboard.writeText(text); }
  catch {
    const input = document.createElement('textarea');
    input.value = text;
    document.body.append(input);
    input.select();
    document.execCommand('copy');
    input.remove();
  }
  notify('Host address copied.');
});

$('#detectPublicNow').addEventListener('click', async () => {
  const button = $('#detectPublicNow');
  button.disabled = true;
  try {
    const response = await fetch('https://api.ipify.org?format=json', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const result = await response.json();
    const address = String(result.ip || '');
    if (!address || !/^[0-9a-fA-F:.]+$/.test(address)) throw new Error();
    const port = Number(new URL(location.href).port || 7399);
    const host = address.includes(':') ? `[${address}]` : address;
    detectedPublicBase = `http://${host}:${port}`;
    try { localStorage.setItem('kinetic.publicHostUrl', detectedPublicBase); } catch { /* storage may be unavailable */ }
    hostBase = detectedPublicBase;
    $('#hostAddress').textContent = hostBase;
    renderRooms();
    notify('Public URL detected. Check it from a device outside your Wi-Fi.');
  } catch {
    notify('Could not detect a public IP. You can enter one in Host settings after updating the Host.');
  } finally {
    button.disabled = false;
  }
});

$('#toggleCode').addEventListener('click', () => {
  const input = $('#settingCode');
  const reveal = input.type === 'password';
  input.type = reveal ? 'text' : 'password';
  $('#toggleCode').textContent = reveal ? 'Hide' : 'Show';
});

$('#detectPublicUrl').addEventListener('click', async () => {
  const button = $('#detectPublicUrl');
  button.disabled = true;
  try {
    const response = await fetch('/api/public-address', { cache: 'no-store' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not detect public address.');
    $('#settingPublicUrl').value = result.publicUrl;
    notify('Public address detected. Verify your router port forward.');
  } catch (error) {
    notify(error.message || 'Could not detect public address.');
  } finally {
    button.disabled = false;
  }
});

$('#saveSettings').addEventListener('click', async () => {
  const button = $('#saveSettings');
  button.disabled = true;
  $('#settingsMessage').textContent = '';
  try {
    const response = await fetch('/api/host/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('#settingName').value,
        accessCode: $('#settingCode').value,
        maxRooms: Number($('#settingCapacity').value),
        publicUrl: $('#settingPublicUrl').value
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not save settings.');
    $('#settingsMessage').textContent = 'Host settings saved.';
    await refresh();
  } catch (error) {
    $('#settingsMessage').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

refresh();
setInterval(refresh, 4000);

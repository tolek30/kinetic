const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let mode = 'host';
let saved = {};

function showStatus(message, success = false) {
  const node = $('#setupStatus');
  node.textContent = message;
  node.hidden = false;
  node.classList.toggle('is-ok', success);
}

function newInviteCode() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const bytes = crypto.getRandomValues(new Uint8Array(10));
  return [...bytes].map((byte) => alphabet[byte % alphabet.length]).join('');
}

function selectMode(next) {
  mode = next;
  $$('.role-card').forEach((card) => {
    const active = card.dataset.mode === next;
    card.classList.toggle('is-active', active);
    card.setAttribute('aria-selected', String(active));
  });
  $$('.role-panel').forEach((panel) => {
    const active = panel.dataset.panel === next;
    panel.classList.toggle('is-active', active);
    panel.hidden = !active;
  });
  $('#setupStatus').hidden = true;
}

async function startHost() {
  const button = $('#startHost');
  button.disabled = true;
  try {
    const result = await window.pywebview.api.start_host(
      $('#hostName').value,
      Number($('#hostPort').value),
      $('#accessCode').value,
      Number($('#maxRooms').value),
      $('#publicUrl').value
    );
    if (!result?.ok) throw new Error(result?.error || 'Host could not start.');
    showStatus('Host is starting…', true);
  } catch (error) {
    showStatus(error.message || 'Host could not start.');
    button.disabled = false;
  }
}

async function joinHost() {
  const button = $('#joinHost');
  button.disabled = true;
  try {
    const result = await window.pywebview.api.join_host(
      $('#hostAddress').value,
      Number($('#instancePort').value),
      $('#instanceCode').value,
      $('#instanceName').value
    );
    if (!result?.ok) throw new Error(result?.error || 'Could not join Host.');
    showStatus('Connected. Opening your cam…', true);
  } catch (error) {
    showStatus(error.message || 'Could not join Host.');
    button.disabled = false;
  }
}

function fillDefaults(values) {
  saved = values || {};
  $('#hostName').value = saved.hostName || 'Stream Cam Host';
  $('#hostPort').value = saved.hostPort || 7399;
  $('#accessCode').value = saved.accessCode || newInviteCode();
  $('#maxRooms').value = String(saved.maxRooms || 10);
  $('#publicUrl').value = saved.publicUrl || '';
  $('#hostAddress').value = saved.hostAddress || '';
  $('#instancePort').value = saved.instancePort || 7399;
  $('#instanceName').value = saved.instanceName || 'My Cam';
  if (saved.lastMode === 'instance') selectMode('instance');
}

$$('.role-card').forEach((card) => card.addEventListener('click', () => selectMode(card.dataset.mode)));
$('#generateCode').addEventListener('click', () => { $('#accessCode').value = newInviteCode(); });
$('#detectPublic').addEventListener('click', async () => {
  const button = $('#detectPublic');
  button.disabled = true;
  try {
    const result = await window.pywebview.api.detect_public_url(Number($('#hostPort').value));
    if (!result?.ok) throw new Error(result?.error || 'Could not detect public address.');
    $('#publicUrl').value = result.publicUrl;
    showStatus('Public address detected. Verify that your router forwards this port.', true);
  } catch (error) {
    showStatus(error.message || 'Could not detect public address.');
  } finally {
    button.disabled = false;
  }
});
$('#startHost').addEventListener('click', startHost);
$('#joinHost').addEventListener('click', joinHost);

window.addEventListener('pywebviewready', async () => {
  try {
    fillDefaults(await window.pywebview.api.get_defaults());
  } catch {
    showStatus('Launch KINETIC with start.cmd to use the desktop app.');
  }
});

if (window.pywebview?.api) {
  window.pywebview.api.get_defaults().then(fillDefaults).catch(() => {});
} else {
  setTimeout(() => {
    if (!window.pywebview?.api) showStatus('This setup screen is meant to run inside the KINETIC desktop app.');
  }, 900);
}

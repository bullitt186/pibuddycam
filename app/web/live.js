/*
 * PiBuddyCam local console: Local monitor (shared snapshot poller) and local WebRTC live video.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { SESSION_STATE, els, handleExpired } from './common.js?v=__ASSET_VERSION__';


/* ------------------------------------------------------------------ */
/* Local monitor (WP-UI5; AC-10/AC-11)                                 */
/*                                                                     */
/* A visibility-aware, authenticated poll of /api/live/frame. The      */
/* server owns the single shared producer; the browser only reads the  */
/* latest frame, so there is no per-viewer decoder and no runaway      */
/* reconnection. Pausing stops polling and lets the viewer lease lapse. */
/* ------------------------------------------------------------------ */

const LIVE_INTERVAL_VISIBLE = 1000;
const LIVE_INTERVAL_HIDDEN = 30000;
const LIVE_MAX_BACKOFF = 30000;
const LIVE_STATUS_INTERVAL = 5000;

const liveMonitor = {
  timer: null,
  controller: null,
  inflight: false,
  paused: false,
  active: false,
  failures: 0,
  objectUrl: null,
  statusAt: 0,
};

function setLiveState(kind, text) {
  if (!els.liveState) return;
  const classes = {
    live: 'chip chip--ok',
    stale: 'chip chip--warn',
    paused: 'chip chip--muted',
    busy: 'chip chip--warn',
    unavailable: 'chip chip--error',
  };
  els.liveState.className = classes[kind] || 'chip chip--muted';
  els.liveState.textContent = text;
}

function showLiveMessage(message) {
  if (els.liveFrame) els.liveFrame.hidden = true;
  if (els.livePlaceholder) {
    els.livePlaceholder.textContent = message;
    els.livePlaceholder.hidden = false;
  }
}

function showLiveFrame(blob, state, age) {
  if (!els.liveFrame) return;
  if (liveMonitor.objectUrl) URL.revokeObjectURL(liveMonitor.objectUrl);
  liveMonitor.objectUrl = URL.createObjectURL(blob);
  els.liveFrame.src = liveMonitor.objectUrl;
  els.liveFrame.hidden = false;
  if (els.livePlaceholder) els.livePlaceholder.hidden = true;
  const ageText = age == null ? '' : ` · ${Number(age).toFixed(1)}s`;
  if (state === 'stale') {
    setLiveState('stale', `Stale snapshot${ageText}`);
  } else {
    setLiveState('live', `Live snapshot${ageText}`);
  }
}

function liveDelay() {
  if (document.visibilityState !== 'visible') return LIVE_INTERVAL_HIDDEN;
  if (liveMonitor.failures <= 0) return LIVE_INTERVAL_VISIBLE;
  const backoff = LIVE_INTERVAL_VISIBLE * Math.pow(2, liveMonitor.failures);
  return Math.min(LIVE_MAX_BACKOFF, backoff);
}

async function refreshLiveStatus() {
  if (!els.liveDetail || !SESSION_STATE.csrf) return;
  const now = Date.now();
  if (liveMonitor.statusAt && now - liveMonitor.statusAt < LIVE_STATUS_INTERVAL) return;
  liveMonitor.statusAt = now;
  try {
    const response = await fetch('/api/live/status', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (!data || data.ok !== true) return;
    if (data.available === false) {
      els.liveDetail.textContent = 'Monitor metrics unavailable.';
      return;
    }
    const producer = data.producer || {};
    const viewers = data.viewers || {};
    const frame = data.frame || {};
    const age = producer.last_frame_age_seconds;
    const ageText = age == null ? '' : ` · frame ${Number(age).toFixed(1)}s`;
    els.liveDetail.textContent =
      `Producer ${producer.running ? 'running' : 'stopped'} · `
      + `${producer.frames_produced || 0} frames · `
      + `${viewers.active || 0}/${viewers.cap || 0} viewers`
      + `${ageText} (${frame.state || 'unknown'})`;
  } catch (_error) {
    /* metrics are best-effort; the frame view is authoritative */
  }
}

export function stopLiveMonitor() {
  liveMonitor.active = false;
  if (liveMonitor.timer !== null) {
    clearTimeout(liveMonitor.timer);
    liveMonitor.timer = null;
  }
  if (liveMonitor.controller) {
    liveMonitor.controller.abort();
    liveMonitor.controller = null;
  }
  liveMonitor.inflight = false;
}

function scheduleLive() {
  if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
  if (!liveMonitor.active || liveMonitor.paused) return;
  liveMonitor.timer = setTimeout(pollLive, liveDelay());
}

export function startLiveMonitor() {
  liveMonitor.active = true;
  liveMonitor.failures = 0;
  if (liveMonitor.paused) return;
  if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
  pollLive();
}

export function syncLiveMonitor() {
  if (!SESSION_STATE.csrf || !liveMonitor.active) {
    stopLiveMonitor();
    return;
  }
  if (document.visibilityState === 'visible') {
    if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
    pollLive();
  } else {
    scheduleLive();
  }
}

async function pollLive() {
  if (!SESSION_STATE.csrf || !liveMonitor.active || liveMonitor.paused) return;
  if (liveMonitor.inflight) {
    scheduleLive();
    return;
  }
  liveMonitor.inflight = true;
  if (liveMonitor.controller) liveMonitor.controller.abort();
  const controller = new AbortController();
  liveMonitor.controller = controller;
  try {
    const response = await fetch('/api/live/frame', {
      credentials: 'same-origin',
      headers: { Accept: 'image/jpeg' },
      signal: controller.signal,
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (response.status === 200) {
      const state = response.headers.get('X-Live-State') || 'live';
      const age = response.headers.get('X-Live-Age');
      const blob = await response.blob();
      if (blob.size > 0) {
        liveMonitor.failures = 0;
        showLiveFrame(blob, state, age);
      } else {
        liveMonitor.failures += 1;
        setLiveState('unavailable', 'No frame');
        showLiveMessage('The local monitor has no frame yet.');
      }
      return;
    }
    if (response.status === 429) {
      liveMonitor.failures += 1;
      setLiveState('busy', 'Too many viewers');
      showLiveMessage('The local monitor is at its viewer limit. Retrying…');
      return;
    }
    const state = response.headers.get('X-Live-State') || 'unavailable';
    liveMonitor.failures += 1;
    if (state === 'stale') {
      setLiveState('stale', 'Stale');
      showLiveMessage('The local monitor frame is stale. Waiting for a fresh frame…');
    } else {
      setLiveState('unavailable', 'Unavailable');
      showLiveMessage('The local monitor is unavailable.');
    }
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    liveMonitor.failures += 1;
    setLiveState('unavailable', 'Unavailable');
    showLiveMessage('Could not reach the local monitor. Retrying…');
  } finally {
    if (liveMonitor.controller === controller) liveMonitor.controller = null;
    liveMonitor.inflight = false;
    refreshLiveStatus();
    if (liveMonitor.active && !liveMonitor.paused) scheduleLive();
  }
}

export function toggleLivePause() {
  liveMonitor.paused = !liveMonitor.paused;
  if (els.liveToggle) {
    els.liveToggle.textContent = liveMonitor.paused ? 'Resume' : 'Pause';
  }
  if (liveMonitor.paused) {
    if (liveMonitor.timer !== null) {
      clearTimeout(liveMonitor.timer);
      liveMonitor.timer = null;
    }
    if (liveMonitor.controller) liveMonitor.controller.abort();
    setLiveState('paused', 'Paused');
    showLiveMessage('Local monitor is paused.');
  } else {
    liveMonitor.failures = 0;
    pollLive();
  }
}

export async function downloadLiveSnapshot() {
  if (els.liveDownload) els.liveDownload.disabled = true;
  try {
    const response = await fetch('/api/live/frame', {
      credentials: 'same-origin',
      headers: { Accept: 'image/jpeg' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (!response.ok) {
      showLiveMessage('No snapshot is available to download right now.');
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'pibuddycam-snapshot.jpg';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  } catch (_error) {
    showLiveMessage('Could not download the current snapshot.');
  } finally {
    if (els.liveDownload) els.liveDownload.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Local WebRTC live video                                             */
/*                                                                     */
/* Manual connect/disconnect over a same-origin WebSocket signaling    */
/* channel (GET /api/live/webrtc). Entirely local: no Prusa cloud      */
/* involvement, and limited to one viewer at a time (server-enforced,  */
/* returned as a `viewer_limit` error/ended reason). Unlike the        */
/* snapshot poller above, a failed session is not auto-retried -- a    */
/* stuck GStreamer pipeline is comparatively expensive to keep         */
/* respawning, so the viewer must click Connect again.                 */
/* ------------------------------------------------------------------ */

const localWebrtc = {
  ws: null,
  pc: null,
  connecting: false,
};

function setWebrtcState(kind, text) {
  if (!els.webrtcState) return;
  const classes = {
    connecting: 'chip chip--warn',
    live: 'chip chip--ok',
    idle: 'chip chip--muted',
    error: 'chip chip--error',
  };
  els.webrtcState.className = classes[kind] || 'chip chip--muted';
  els.webrtcState.textContent = text;
}

function showWebrtcMessage(message) {
  if (els.webrtcVideo) els.webrtcVideo.hidden = true;
  if (els.webrtcPlaceholder) {
    els.webrtcPlaceholder.textContent = message;
    els.webrtcPlaceholder.hidden = false;
  }
}

function setWebrtcConnectLabel() {
  if (!els.webrtcConnect) return;
  els.webrtcConnect.disabled = false;
  if (localWebrtc.connecting) {
    els.webrtcConnect.textContent = 'Cancel';
  } else if (localWebrtc.pc || localWebrtc.ws) {
    els.webrtcConnect.textContent = 'Disconnect';
  } else {
    els.webrtcConnect.textContent = 'Connect';
  }
}

function teardownLocalWebrtc() {
  if (localWebrtc.pc) {
    localWebrtc.pc.close();
    localWebrtc.pc = null;
  }
  if (localWebrtc.ws) {
    const ws = localWebrtc.ws;
    localWebrtc.ws = null;
    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: 'stop' }));
      } catch (_error) {
        /* best-effort; the socket is being closed either way */
      }
    }
    ws.close();
  }
  if (els.webrtcVideo) els.webrtcVideo.srcObject = null;
  localWebrtc.connecting = false;
}

export function disconnectLocalWebrtc() {
  if (!localWebrtc.pc && !localWebrtc.ws && !localWebrtc.connecting) return;
  teardownLocalWebrtc();
  setWebrtcState('idle', 'Idle');
  showWebrtcMessage('Live video is not connected.');
  if (els.webrtcDetail) els.webrtcDetail.textContent = '';
  setWebrtcConnectLabel();
}

function webrtcEndedMessage(reason) {
  if (reason === 'viewer_limit') return 'Live video is at its viewer limit.';
  if (reason === 'ice-failed' || reason === 'no-ice-connection' || reason === 'ice-disconnected') {
    return 'Could not establish the video connection.';
  }
  return 'Live video session ended.';
}

function webrtcErrorMessage(code) {
  if (code === 'viewer_limit') return 'Live video is at its viewer limit.';
  return 'Live video signaling error.';
}

function failLocalWebrtc(message) {
  teardownLocalWebrtc();
  setWebrtcState('error', 'Disconnected');
  showWebrtcMessage(message);
  if (els.webrtcDetail) els.webrtcDetail.textContent = message;
  setWebrtcConnectLabel();
}

function connectLocalWebrtc() {
  if (!SESSION_STATE.csrf) return;
  localWebrtc.connecting = true;
  setWebrtcState('connecting', 'Connecting…');
  showWebrtcMessage('Connecting to the camera…');
  if (els.webrtcDetail) els.webrtcDetail.textContent = '';
  setWebrtcConnectLabel();

  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${scheme}//${location.host}/api/live/webrtc`);
  localWebrtc.ws = ws;

  const pc = new RTCPeerConnection();
  localWebrtc.pc = pc;

  pc.ontrack = (event) => {
    if (els.webrtcVideo) {
      els.webrtcVideo.srcObject = event.streams[0];
      els.webrtcVideo.hidden = false;
    }
    if (els.webrtcPlaceholder) els.webrtcPlaceholder.hidden = true;
  };

  pc.onicecandidate = (event) => {
    if (event.candidate && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'ice',
        candidate: event.candidate.candidate,
        sdpMLineIndex: event.candidate.sdpMLineIndex || 0,
      }));
    }
  };

  pc.oniceconnectionstatechange = () => {
    if (localWebrtc.pc !== pc) return;
    const state = pc.iceConnectionState;
    if (state === 'connected' || state === 'completed') {
      localWebrtc.connecting = false;
      setWebrtcState('live', 'Live');
      setWebrtcConnectLabel();
    } else if (state === 'failed' || state === 'closed') {
      failLocalWebrtc('Connection lost.');
    }
  };

  ws.addEventListener('message', async (event) => {
    if (localWebrtc.ws !== ws) return;
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (_error) {
      return;
    }
    if (!message || typeof message.type !== 'string') return;
    if (message.type === 'offer') {
      try {
        await pc.setRemoteDescription({ type: 'offer', sdp: message.sdp });
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        ws.send(JSON.stringify({ type: 'answer', sdp: answer.sdp }));
      } catch (_error) {
        failLocalWebrtc('Could not negotiate the video session.');
      }
    } else if (message.type === 'ice') {
      try {
        await pc.addIceCandidate({
          candidate: message.candidate,
          sdpMLineIndex: message.sdpMLineIndex || 0,
        });
      } catch (_error) {
        /* a late/duplicate candidate is not fatal */
      }
    } else if (message.type === 'ended') {
      failLocalWebrtc(webrtcEndedMessage(message.reason));
    } else if (message.type === 'error') {
      failLocalWebrtc(webrtcErrorMessage(message.code));
    }
  });

  ws.addEventListener('close', () => {
    if (localWebrtc.ws === ws) failLocalWebrtc('Signaling connection closed.');
  });

  ws.addEventListener('error', () => {
    if (localWebrtc.ws === ws) {
      failLocalWebrtc('Could not reach the local signaling endpoint.');
    }
  });
}

export function toggleLocalWebrtc() {
  if (localWebrtc.pc || localWebrtc.ws || localWebrtc.connecting) {
    disconnectLocalWebrtc();
  } else {
    connectLocalWebrtc();
  }
}
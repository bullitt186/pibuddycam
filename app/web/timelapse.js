/*
 * PiBuddyCam local console: Timelapse library, print sessions and the clock warning.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { SESSION_STATE, els, formatBytes, formatMediaTimestamp, handleExpired, request, setText } from './common.js?v=__ASSET_VERSION__';
import { gpioEl } from './gpio.js?v=__ASSET_VERSION__';


/* ------------------------------------------------------------------ */
/* Timelapse library (WP-UI6; AC-12/AC-13)                             */
/* ------------------------------------------------------------------ */

const TIMELAPSE_PAGE_SIZE = 10;
const TIMELAPSE_FRAME_PAGE_SIZE = 12;
const TIMELAPSE_JOB_POLL_MS = 1500;

const timelapse = {
  page: 1,
  status: 'all',
  pages: 0,
  total: 0,
  bytesTotal: 0,
  framesPage: 1,
  framesPages: 0,
  framesTotal: 0,
  framesBytesTotal: 0,
  controller: null,
  inflight: false,
  jobTimer: null,
  jobId: null,
  loadedOnce: false,
  session: '',
};

function setTimelapseMessage(kind, text) {
  if (!els.timelapsesState) return;
  if (!text) {
    els.timelapsesState.textContent = '';
    els.timelapsesState.hidden = true;
    return;
  }
  els.timelapsesState.className = `overview-state overview-state--${kind}`;
  els.timelapsesState.textContent = text;
  els.timelapsesState.hidden = false;
}

function setTimelapseBuildStatus(text) {
  if (els.timelapseBuildStatus) els.timelapseBuildStatus.textContent = text || '';
}

export async function loadTimelapses() {
  if (!SESSION_STATE.csrf) return;
  if (!timelapse.loadedOnce) {
    setTimelapseMessage('loading', 'Loading the timelapse library…');
  }
  let ok = false;
  try {
    const [videos, frames] = await Promise.all([
      fetchTimelapseVideos(),
      fetchTimelapseFrames(),
    ]);
    ok = videos && frames;
  } catch (_error) {
    ok = false;
  }
  if (!ok) {
    setTimelapseMessage('error', 'Could not load the timelapse library. Try Refresh.');
    return;
  }
  timelapse.loadedOnce = true;
  setTimelapseMessage('', '');
  updateTimelapseStats();
}

async function fetchTimelapseVideos() {
  if (timelapse.controller) timelapse.controller.abort();
  const controller = new AbortController();
  timelapse.controller = controller;
  const params = new URLSearchParams({
    page: String(timelapse.page),
    page_size: String(TIMELAPSE_PAGE_SIZE),
  });
  if (timelapse.status !== 'all') params.set('status', timelapse.status);
  try {
    const response = await fetch(`/api/media/timelapses?${params.toString()}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return false;
    }
    if (!response.ok) return false;
    const data = await response.json();
    if (!data || data.ok !== true) return false;
    renderTimelapseVideos(data);
    return true;
  } catch (error) {
    if (error && error.name === 'AbortError') return false;
    return false;
  } finally {
    if (timelapse.controller === controller) timelapse.controller = null;
  }
}

async function fetchTimelapseFrames() {
  const params = new URLSearchParams({
    page: String(timelapse.framesPage),
    page_size: String(TIMELAPSE_FRAME_PAGE_SIZE),
  });
  if (timelapse.session) params.set('session', timelapse.session);
  try {
    const response = await fetch(`/api/media/frames?${params.toString()}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return false;
    }
    if (!response.ok) return false;
    const data = await response.json();
    if (!data || data.ok !== true) return false;
    renderTimelapseFrames(data);
    return true;
  } catch (_error) {
    return false;
  }
}

function updateTimelapseStats() {
  setText(els.timelapseVideoCount, String(timelapse.total));
  setText(els.timelapseFrameCount, String(timelapse.framesTotal));
  setText(
    els.timelapseLibrarySize,
    formatBytes(timelapse.bytesTotal + timelapse.framesBytesTotal));
}

function renderTimelapseVideos(data) {
  timelapse.total = Number(data.total) || 0;
  timelapse.pages = Number(data.pages) || 0;
  timelapse.bytesTotal = Number(data.bytes_total) || 0;
  const items = Array.isArray(data.items) ? data.items : [];
  const body = els.timelapseVideosBody;
  if (body) body.textContent = '';
  items.forEach((item) => {
    if (!body) return;
    const row = document.createElement('tr');
    const nameCell = document.createElement('td');
    nameCell.textContent = item.name;
    row.appendChild(nameCell);

    const statusCell = document.createElement('td');
    const chip = document.createElement('span');
    chip.className = `chip chip--${statusChipKind(item.status_label)}`;
    chip.textContent = item.status_label || 'unknown';
    statusCell.appendChild(chip);
    row.appendChild(statusCell);

    const sizeCell = document.createElement('td');
    sizeCell.textContent = formatBytes(item.size);
    row.appendChild(sizeCell);

    const createdCell = document.createElement('td');
    createdCell.textContent = formatMediaTimestamp(item.modified);
    row.appendChild(createdCell);

    const playCell = document.createElement('td');
    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'btn btn--small';
    play.textContent = 'Play';
    play.addEventListener('click', () => playTimelapse(item.name));
    playCell.appendChild(play);
    row.appendChild(playCell);

    const downloadCell = document.createElement('td');
    const link = document.createElement('a');
    link.className = 'btn btn--small';
    link.href = item.download_url || `/api/media/timelapses/${encodeURIComponent(item.name)}`;
    link.setAttribute('download', item.name);
    link.textContent = 'Download';
    downloadCell.appendChild(link);
    row.appendChild(downloadCell);
    body.appendChild(row);
  });
  const hasItems = items.length > 0;
  if (els.timelapseVideos) els.timelapseVideos.hidden = !hasItems;
  if (els.timelapseVideosEmpty) els.timelapseVideosEmpty.hidden = hasItems;
  renderPager(
    els.timelapseVideosPager, els.timelapseVideosPage, els.timelapseVideosPrev,
    els.timelapseVideosNext, timelapse.page, timelapse.pages, timelapse.total);
}

function statusChipKind(label) {
  if (label === 'completed') return 'ok';
  if (label === 'error') return 'error';
  if (label === 'pending') return 'warn';
  return 'muted';
}

function renderTimelapseFrames(data) {
  timelapse.framesTotal = Number(data.total) || 0;
  timelapse.framesPages = Number(data.pages) || 0;
  timelapse.framesBytesTotal = Number(data.bytes_total) || 0;
  const items = Array.isArray(data.items) ? data.items : [];
  const grid = els.timelapseFramesGrid;
  if (grid) grid.textContent = '';
  items.forEach((item) => {
    if (!grid) return;
    const figure = document.createElement('figure');
    figure.className = 'media-grid__item';
    const link = document.createElement('a');
    link.href = item.download_url || `/api/media/frames/${encodeURIComponent(item.name)}`;
    link.setAttribute('download', item.name);
    const image = document.createElement('img');
    image.loading = 'lazy';
    image.alt = `Timelapse frame ${item.name}`;
    image.src = item.preview_url || `/api/media/frames/${encodeURIComponent(item.name)}`;
    link.appendChild(image);
    figure.appendChild(link);
    const caption = document.createElement('figcaption');
    caption.textContent = `${item.name} · ${formatBytes(item.size)}`;
    figure.appendChild(caption);
    grid.appendChild(figure);
  });
  const hasItems = items.length > 0;
  if (els.timelapseFramesEmpty) els.timelapseFramesEmpty.hidden = hasItems;
  renderPager(
    els.timelapseFramesPager, els.timelapseFramesPage, els.timelapseFramesPrev,
    els.timelapseFramesNext, timelapse.framesPage, timelapse.framesPages,
    timelapse.framesTotal);
}

function renderPager(pager, label, prev, next, page, pages, total) {
  const show = total > 0 && pages > 1;
  if (pager) pager.hidden = !show;
  if (label) label.textContent = show ? `Page ${page} of ${pages}` : '';
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = pages === 0 || page >= pages;
}

function selectTimelapseFilter(status) {
  timelapse.status = status;
  timelapse.page = 1;
  if (els.timelapseFilters) {
    els.timelapseFilters.querySelectorAll('[data-timelapse-filter]').forEach((button) => {
      button.setAttribute(
        'aria-pressed', button.dataset.timelapseFilter === status ? 'true' : 'false');
    });
  }
  fetchTimelapseVideos();
}

function changeTimelapsePage(delta) {
  const next = timelapse.page + delta;
  if (next < 1 || (timelapse.pages && next > timelapse.pages)) return;
  timelapse.page = next;
  fetchTimelapseVideos();
}

function changeTimelapseFramePage(delta) {
  const next = timelapse.framesPage + delta;
  if (next < 1 || (timelapse.framesPages && next > timelapse.framesPages)) return;
  timelapse.framesPage = next;
  fetchTimelapseFrames();
}

function timelapsePlaybackSupported() {
  try {
    const probe = document.createElement('video');
    return Boolean(probe.canPlayType && probe.canPlayType('video/x-msvideo'));
  } catch (_error) {
    return false;
  }
}

function playTimelapse(name) {
  if (!timelapsePlaybackSupported()) {
    setTimelapseBuildStatus(
      'This browser cannot play MJPEG AVI inline. Use Download instead.');
    return;
  }
  window.open(
    `/api/media/timelapses/${encodeURIComponent(name)}`, '_blank', 'noopener');
}

async function buildTimelapse() {
  if (!SESSION_STATE.csrf) return;
  if (els.timelapseBuild) els.timelapseBuild.disabled = true;
  setTimelapseBuildStatus('Queuing a build…');
  const buildBody = timelapse.session ? { session: timelapse.session } : {};
  const result = await request('/api/media/timelapses/build', {
    method: 'POST', csrf: true, body: buildBody,
  });
  if (result.status === 401) {
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    handleExpired();
    return;
  }
  if (result.status === 409) {
    setTimelapseBuildStatus('A build is already running. Waiting for it to finish…');
    if (result.data && result.data.job_id) pollTimelapseBuild(result.data.job_id);
    return;
  }
  if (!result.ok || !result.data || result.data.ok !== true) {
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    const reason = result.data && result.data.error
      ? result.data.error : 'The build could not be started.';
    setTimelapseBuildStatus(reason);
    return;
  }
  const job = result.data.job || {};
  setTimelapseBuildStatus(`Build ${job.state || 'pending'} — 0/${job.frames_total || 0} frames`);
  pollTimelapseBuild(job.id);
}

export function stopTimelapsePolling() {
  if (timelapse.jobTimer !== null) {
    clearTimeout(timelapse.jobTimer);
    timelapse.jobTimer = null;
  }
}

async function pollTimelapseBuild(jobId) {
  if (!jobId) return;
  timelapse.jobId = jobId;
  stopTimelapsePolling();
  const result = await request(`/api/media/jobs/${encodeURIComponent(jobId)}`);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (!result.ok || !result.data || result.data.ok !== true) {
    setTimelapseBuildStatus('The build status is unavailable.');
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    return;
  }
  const job = result.data.job || {};
  if (job.state === 'pending' || job.state === 'running') {
    setTimelapseBuildStatus(
      `Building — ${job.frames_written || 0}/${job.frames_total || 0} frames`);
    timelapse.jobTimer = setTimeout(() => pollTimelapseBuild(jobId), TIMELAPSE_JOB_POLL_MS);
    return;
  }
  timelapse.jobId = null;
  if (els.timelapseBuild) els.timelapseBuild.disabled = false;
  if (job.state === 'done') {
    setTimelapseBuildStatus(
      `Build complete — ${job.frames_total || 0} frames assembled.`);
  } else {
    setTimelapseBuildStatus(`Build failed: ${job.reason || 'unknown error'}.`);
  }
  timelapse.page = 1;
  fetchTimelapseVideos();
  fetchTimelapseFrames();
}

export function wireTimelapse() {
  wireTimelapseSession();
  if (els.timelapseBuild) els.timelapseBuild.addEventListener('click', buildTimelapse);
  if (els.timelapseRefresh) {
    els.timelapseRefresh.addEventListener('click', () => loadTimelapses());
  }
  if (els.timelapseFilters) {
    els.timelapseFilters.addEventListener('click', (event) => {
      const button = event.target.closest('[data-timelapse-filter]');
      if (button) selectTimelapseFilter(button.dataset.timelapseFilter);
    });
  }
  if (els.timelapseVideosPrev) {
    els.timelapseVideosPrev.addEventListener('click', () => changeTimelapsePage(-1));
  }
  if (els.timelapseVideosNext) {
    els.timelapseVideosNext.addEventListener('click', () => changeTimelapsePage(1));
  }
  if (els.timelapseFramesPrev) {
    els.timelapseFramesPrev.addEventListener('click', () => changeTimelapseFramePage(-1));
  }
  if (els.timelapseFramesNext) {
    els.timelapseFramesNext.addEventListener('click', () => changeTimelapseFramePage(1));
  }
}

/* ------------------------------------------------------------------ */
/* Timelapses view: print sessions and the clock warning               */
/* ------------------------------------------------------------------ */

export async function loadTimelapseExtras() {
  if (!SESSION_STATE.csrf) return;
  const [sessions, network] = await Promise.all([
    request('/api/media/sessions'),
    request('/api/network'),
  ]);
  if (sessions.status === 401 || network.status === 401) {
    handleExpired();
    return;
  }
  const warning = gpioEl('timelapse-clock-warning');
  if (warning) {
    const time = network.ok && network.data && network.data.time;
    warning.hidden = !(time && time.synchronized === false);
  }
  if (sessions.ok && sessions.data && Array.isArray(sessions.data.sessions)) {
    renderSessionPicker(sessions.data);
  }
}

function renderSessionPicker(data) {
  const select = gpioEl('timelapse-session');
  if (!select) return;
  select.textContent = '';
  const loose = document.createElement('option');
  loose.value = '';
  loose.textContent = 'Loose frames (interval or no session)';
  select.appendChild(loose);
  data.sessions.forEach((session) => {
    const option = document.createElement('option');
    option.value = session.name;
    const suffix = session.video ? ' · video built' : '';
    const live = data.active === session.name ? ' · recording' : '';
    option.textContent = `${session.name} · ${session.frames} frames${suffix}${live}`;
    select.appendChild(option);
  });
  const known = data.sessions.some((session) => session.name === timelapse.session);
  if (!known) timelapse.session = '';
  select.value = timelapse.session;
  updateSessionInfo();
}

function updateSessionInfo() {
  const info = gpioEl('timelapse-session-info');
  if (!info) return;
  info.textContent = timelapse.session
    ? 'Showing the frames of one print. Build video assembles this session.'
    : '';
}

function wireTimelapseSession() {
  const select = gpioEl('timelapse-session');
  if (!select) return;
  select.addEventListener('change', () => {
    timelapse.session = select.value;
    timelapse.framesPage = 1;
    updateSessionInfo();
    fetchTimelapseFrames();
  });
}
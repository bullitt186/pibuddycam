#!/usr/bin/env node
/*
 * WP-UI8 (AC-18/AC-19): Playwright end-to-end runner for the local Web console.
 *
 * Runs the deterministic harness in tests/e2e/server.py (real admin core and
 * assets, injected fakes, synthetic state) against a real Chromium at a desktop
 * and a 360px mobile viewport. It is opt-in: if the Playwright browser binary
 * is unavailable it prints SKIP and exits 0 unless E2E_REQUIRED=1.
 *
 * Usage:
 *   node tests/e2e/run.mjs [--headed] [--grep <substring>]
 * or the wrapper:
 *   tests/e2e/run.sh
 *
 * Playwright is resolved from the default module path and the system-global
 * roots; a user-local install is picked up by setting NODE_PATH (e.g.
 * NODE_PATH="$HOME/.local/lib/node_modules" tests/e2e/run.sh).
 */
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SERVER = path.join(__dirname, 'server.py');
const ARTIFACTS = path.join(__dirname, '.artifacts');
const PASSWORD = 'e2e-admin-password-not-a-secret';
const BOOT_TIMEOUT_MS = 20000;

const args = process.argv.slice(2);
const headed = args.includes('--headed');
const grepIndex = args.indexOf('--grep');
const grep = grepIndex >= 0 ? args[grepIndex + 1] : null;

/* ------------------------------------------------------------------ */
/* Playwright resolution (global install; no package.json dependency)  */
/* ------------------------------------------------------------------ */
async function loadPlaywright() {
  if (process.env.E2E_FORCE_NO_BROWSER === '1') return null;
  const require = createRequire(import.meta.url);
  // Search the default resolution path plus the common system-global module
  // roots. A user-local install is covered by setting NODE_PATH (documented in
  // the header comment), so no personal path is hardcoded here.
  const bases = [
    null,
    '/usr/lib/node_modules',
    '/usr/local/lib/node_modules',
    ...(process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean),
  ];
  for (const base of bases) {
    try {
      const resolved = base ? path.join(base, 'playwright') : 'playwright';
      const mod = require(resolved);
      if (mod && mod.chromium) return mod;
    } catch (_error) {
      /* try the next candidate */
    }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* tiny assertion helpers                                              */
/* ------------------------------------------------------------------ */
class AssertionError extends Error {}

function assert(condition, message) {
  if (!condition) throw new AssertionError(message);
}

// Polls a locator count. `page.waitForFunction` evaluates a string in the page,
// which the console's CSP (`script-src 'self'`) refuses on some Playwright builds.
async function waitForCount(locator, predicate, timeout = 8000) {
  const deadline = Date.now() + timeout;
  let count = await locator.count();
  while (!predicate(count)) {
    if (Date.now() > deadline) throw new AssertionError(`count never matched, last ${count}`);
    await new Promise((resolve) => setTimeout(resolve, 100));
    count = await locator.count();
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new AssertionError(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function assertIncludes(haystack, needle, message) {
  if (!String(haystack).includes(needle)) {
    throw new AssertionError(`${message}: ${JSON.stringify(String(haystack))} does not include ${JSON.stringify(needle)}`);
  }
}

async function waitForText(locator, needle, timeout = 8000) {
  const deadline = Date.now() + timeout;
  let last = '';
  while (Date.now() < deadline) {
    try {
      last = (await locator.first().textContent()) || '';
      if (last.includes(needle)) return last;
    } catch (_error) {
      last = '';
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new AssertionError(`timed out waiting for text ${JSON.stringify(needle)}; last=${JSON.stringify(last)}`);
}

async function waitForVisible(locator, timeout = 8000) {
  try {
    await locator.first().waitFor({ state: 'visible', timeout });
  } catch (_error) {
    throw new AssertionError(`timed out waiting for an element to become visible`);
  }
}

/* ------------------------------------------------------------------ */
/* server lifecycle                                                    */
/* ------------------------------------------------------------------ */
function startServer() {
  return new Promise((resolve, reject) => {
    const child = spawn('python3', [SERVER, '--host', '127.0.0.1', '--port', '0'], {
      cwd: REPO_ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`E2E server did not become ready.\nstdout:${out}\nstderr:${err}`));
    }, BOOT_TIMEOUT_MS);
    child.stdout.on('data', (chunk) => {
      out += chunk.toString();
      const match = out.match(/E2E_READY (\S+)/);
      if (match) {
        clearTimeout(timer);
        resolve({ child, base: match[1] });
      }
    });
    child.stderr.on('data', (chunk) => { err += chunk.toString(); });
    child.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`E2E server exited early (code ${code}).\nstdout:${out}\nstderr:${err}`));
    });
  });
}

function stopServer(child) {
  return new Promise((resolve) => {
    if (!child || child.exitCode !== null) return resolve();
    const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000);
    child.on('exit', () => { clearTimeout(timer); resolve(); });
    child.kill('SIGTERM');
  });
}

// Never leave an orphaned harness if the runner is interrupted.
function installCleanup(child) {
  const kill = () => { try { child.kill('SIGTERM'); } catch (_error) { /* gone */ } };
  process.once('exit', kill);
  process.once('SIGINT', () => { kill(); process.exit(130); });
  process.once('SIGTERM', () => { kill(); process.exit(143); });
}

/* ------------------------------------------------------------------ */
/* shared test utilities                                               */
/* ------------------------------------------------------------------ */
async function reset(request, base, options = {}) {
  const response = await request.post(`${base}/__e2e/reset`, { data: options });
  assert(response.ok(), 'e2e reset failed');
}

async function setScenario(request, base, scenarios) {
  await request.post(`${base}/__e2e/scenario`, { data: scenarios });
}

async function harnessState(request, base) {
  const response = await request.get(`${base}/__e2e/counters`);
  return response.json();
}

async function counters(request, base) {
  return (await harnessState(request, base)).counters;
}

async function login(page, base) {
  await page.goto(`${base}/admin`);
  await page.locator('#password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await waitForVisible(page.locator('#app-view'));
}

async function openView(page, name) {
  await page.getByRole('button', { name, exact: true }).click();
  await waitForVisible(page.locator(`[data-view-panel="${name.toLowerCase()}"]`));
}

async function completeReauth(page) {
  await waitForVisible(page.locator('#reauth-dialog[open]'));
  await page.locator('#reauth-password').fill(PASSWORD);
  await page.locator('#reauth-confirm').click();
}

async function setupScreen(page, name) {
  await waitForVisible(page.locator(`[data-screen="${name}"]`));
}

async function setupToWifi(page, base) {
  await page.goto(`${base}/`);
  await setupScreen(page, 'welcome');
  await waitForText(page.locator('#status-camera'), 'Detected');
  await page.locator('#welcome-next').click();
  await setupScreen(page, 'wifi');
}

/* ------------------------------------------------------------------ */
/* tests                                                               */
/* ------------------------------------------------------------------ */
const TESTS = [
  {
    name: 'login failure/success/logout and session expiry',
    async run({ page, base, request }) {
      await page.goto(`${base}/admin`);
      await page.locator('#password').fill('wrong-password');
      await page.getByRole('button', { name: 'Sign in' }).click();
      await waitForText(page.locator('#login-error'), 'Incorrect password');

      await page.locator('#password').fill(PASSWORD);
      await page.getByRole('button', { name: 'Sign in' }).click();
      await waitForVisible(page.locator('#app-view'));
      assertIncludes(await page.locator('#session-state').textContent(), 'Signed in', 'session chip');

      await page.locator('#logout').click();
      await waitForVisible(page.locator('#login-view'));

      // Session expiry: revoking the session makes the next authenticated poll
      // (dashboard/live-monitor) return 401 and the console returns to login.
      await login(page, base);
      await request.post(`${base}/__e2e/expire`, { data: {} });
      await waitForText(page.locator('#login-error'), 'session expired', 15000);
      await waitForVisible(page.locator('#login-view'));
    },
  },
  {
    name: 'navigation switches panels and updates aria-current',
    async run({ page, base }) {
      await login(page, base);
      for (const [button, panel] of [
        ['Camera', 'camera'],
        ['Timelapses', 'timelapses'],
        ['Integrations', 'integrations'],
        ['System', 'system'],
        ['Overview', 'overview'],
      ]) {
        await openView(page, button);
        assertEqual(
          await page.locator(`.nav__item[data-view="${panel}"]`).getAttribute('aria-current'),
          'page',
          `${panel} aria-current`,
        );
        assert(
          await page.locator(`[data-view-panel="${panel}"]`).isVisible(),
          `${panel} panel visible`,
        );
      }
    },
  },
  {
    name: 'overview dashboard and local monitor pause/resume/download/error',
    async run({ page, base, request }) {
      await login(page, base);
      await waitForVisible(page.locator('#overview-content'));
      assertIncludes(await page.locator('#metric-version').textContent(), '1.2.3-e2e', 'version metric');
      assertEqual(await page.locator('#overview-status .status-chips__item').count(), 8, 'status chips');

      await waitForVisible(page.locator('#live-frame'));
      assertIncludes(await page.locator('#live-state').textContent(), 'Live snapshot', 'live chip');

      await page.locator('#live-toggle').click();
      await waitForText(page.locator('#live-state'), 'Paused');
      assertIncludes(await page.locator('#live-placeholder').textContent(), 'paused', 'paused placeholder');

      await page.locator('#live-toggle').click();
      await waitForVisible(page.locator('#live-frame'));

      const [download] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('#live-download').click(),
      ]);
      assertEqual(download.suggestedFilename(), 'pibuddycam-snapshot.jpg', 'snapshot filename');

      await setScenario(request, base, { live: 'stale' });
      await waitForText(page.locator('#live-placeholder'), 'stale', 10000);
      assertIncludes(await page.locator('#live-state').textContent(), 'Stale', 'stale chip');
    },
  },
  {
    // Frontend state-machine coverage only: the harness serves the real
    // admin_http.AdminApp over stdlib http.server, which has no aiohttp
    // WebSocket support, so /api/live/webrtc is not reachable here (see
    // local_webrtc_signaling's module docstring). window.WebSocket is
    // replaced before navigation so the Connect/Cancel/Disconnect and error
    // UI states can be exercised deterministically without a real signaling
    // backend or a fabricated SDP negotiation.
    name: 'local WebRTC connect/cancel/viewer-limit UI state machine',
    async run({ page, base }) {
      await page.addInitScript(() => {
        window.__wsInstances = [];
        class FakeWebSocket extends EventTarget {
          constructor(url) {
            super();
            this.url = url;
            this.readyState = FakeWebSocket.OPEN;
            this.sent = [];
            window.__wsInstances.push(this);
            window.__lastWs = this;
          }

          send(data) {
            this.sent.push(data);
          }

          close() {
            this.readyState = FakeWebSocket.CLOSED;
            this.dispatchEvent(new Event('close'));
          }

          // Test-only helper: simulate one server -> browser signaling frame.
          __serverSend(message) {
            this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(message) }));
          }
        }
        FakeWebSocket.CONNECTING = 0;
        FakeWebSocket.OPEN = 1;
        FakeWebSocket.CLOSING = 2;
        FakeWebSocket.CLOSED = 3;
        window.WebSocket = FakeWebSocket;
      });

      await login(page, base);
      await waitForVisible(page.locator('#overview-content'));

      assertIncludes(
        await page.locator('#live-webrtc-placeholder').textContent(),
        'not connected',
        'initial live video placeholder',
      );

      await page.locator('#live-webrtc-connect').click();
      await waitForText(page.locator('#live-webrtc-state'), 'Connecting');
      assertEqual(
        await page.locator('#live-webrtc-connect').textContent(), 'Cancel',
        'connect button becomes Cancel while connecting',
      );
      assertEqual(
        await page.evaluate(() => window.__wsInstances.length), 1,
        'one signaling WebSocket opened',
      );

      await page.evaluate(() => window.__lastWs.__serverSend({ type: 'error', code: 'viewer_limit' }));
      await waitForText(page.locator('#live-webrtc-state'), 'Disconnected');
      assertIncludes(
        await page.locator('#live-webrtc-detail').textContent(), 'viewer limit',
        'viewer-limit detail message',
      );
      assertEqual(
        await page.locator('#live-webrtc-connect').textContent(), 'Connect',
        'connect button resets after the server rejects the viewer',
      );

      await page.locator('#live-webrtc-connect').click();
      await waitForText(page.locator('#live-webrtc-state'), 'Connecting');
      assertEqual(
        await page.evaluate(() => window.__wsInstances.length), 2,
        'a fresh signaling WebSocket opens on reconnect',
      );

      await page.locator('#live-webrtc-connect').click();
      await waitForText(page.locator('#live-webrtc-state'), 'Idle');
      assertEqual(
        await page.evaluate(() => window.__lastWs.readyState), 3,
        'cancelling closes the in-flight signaling WebSocket',
      );
      assertEqual(
        await page.locator('#live-webrtc-connect').textContent(), 'Connect',
        'connect button resets after cancel',
      );
    },
  },
  {
    name: 'camera settings converge authoritatively and TURN lock is honest',
    async run({ page, base }) {
      await login(page, base);
      await openView(page, 'Camera');
      assertEqual(await page.locator('#camera-name').inputValue(), 'E2E Camera', 'camera name');

      const qualityForm = page.locator('.setting-form[data-setting="quality"]');
      assert(await qualityForm.locator('input[value="hd"]').isChecked(), 'HD is authoritative');

      await qualityForm.locator('input[value="fhd"]').check();
      await qualityForm.getByRole('button', { name: 'Apply quality' }).click();
      await waitForText(qualityForm.locator('.form-status'), 'TURN');
      assert(await qualityForm.locator('input[value="hd"]').isChecked(), 'TURN rejection restores HD');

      await qualityForm.locator('input[value="sd"]').check();
      await qualityForm.getByRole('button', { name: 'Apply quality' }).click();
      await waitForText(qualityForm.locator('.form-status'), 'Saved');
      assert(await qualityForm.locator('input[value="sd"]').isChecked(), 'accepted quality is SD');

      const rotationForm = page.locator('.setting-form[data-setting="rotation"]');
      const rotationWarning = page.locator('#rotation-warning');
      assert(await rotationForm.locator('input[value="0"]').isChecked(), '0° is authoritative');
      assert(await rotationWarning.isHidden(), 'no warning at 0°');
      await rotationForm.locator('input[value="90"]').check();
      assert(await rotationWarning.isVisible(), '90° shows the cost warning');
      await rotationForm.getByRole('button', { name: 'Apply rotation' }).click();
      await waitForText(rotationForm.locator('.form-status'), 'Saved');
      assert(await rotationForm.locator('input[value="90"]').isChecked(), 'accepted rotation is 90°');
      await rotationForm.locator('input[value="180"]').check();
      assert(await rotationWarning.isHidden(), 'no warning at 180°');

      const intervalForm = page.locator('.setting-form[data-setting="snapshot_interval"]');
      await page.locator('#snapshot-interval').fill('5');
      await intervalForm.getByRole('button', { name: 'Save interval' }).click();
      await waitForText(intervalForm.locator('.form-status'), 'between 10 and 600');
    },
  },
  {
    name: 'MQTT test/save-anyway/reauth/stored credentials',
    async run({ page, base, request }) {
      await login(page, base);
      await openView(page, 'Integrations');
      await waitForVisible(page.locator('#mqtt-form'));

      await page.locator('#mqtt-uri').fill('mqtts://broker.e2e.invalid:8883');
      await page.locator('#mqtt-username').fill('e2e-user');
      await page.locator('#mqtt-password').fill('e2e-broker-password');
      await page.locator('#mqtt-test').click();
      await waitForText(page.locator('#mqtt-form .form-status'), 'Connection test succeeded');

      // Editing a tested input invalidates the success; save is refused.
      await page.locator('#mqtt-uri').fill('mqtts://broker2.e2e.invalid:8883');
      await page.locator('#mqtt-save').click();
      await waitForText(page.locator('#mqtt-form .form-status'), 'Test the connection first');

      // Save anyway still requires a fresh re-auth.
      await page.locator('#mqtt-save-anyway').check();
      await page.locator('#mqtt-save').click();
      await completeReauth(page);
      await waitForText(page.locator('#mqtt-form .form-status'), 'restart');
      await waitForText(page.locator('#mqtt-username-state'), 'Username configured');

      // A blank replacement field now tests the stored credential server-side.
      await page.locator('#mqtt-uri').fill('mqtts://broker3.e2e.invalid:8883');
      await page.locator('#mqtt-test').click();
      await waitForText(page.locator('#mqtt-form .form-status'), 'Connection test succeeded');
      const after = await counters(request, base);
      assert(after.mqtt_test_stored_password >= 1, 'stored broker password was used for the test');

      // A failed probe must not claim success and the UI offers the override.
      await setScenario(request, base, { mqtt_test: 'fail' });
      await page.locator('#mqtt-uri').fill('mqtts://broker4.e2e.invalid:8883');
      await page.locator('#mqtt-test').click();
      await waitForText(page.locator('#mqtt-form .form-status'), 'Connection test failed');
      assertIncludes(
        await page.locator('#mqtt-form .form-status').textContent(),
        'Save anyway',
        'failed test offers the explicit override',
      );
    },
  },
  {
    name: 'Prusa redaction/reauth warning/save',
    async run({ page, base }) {
      await login(page, base);
      await openView(page, 'Integrations');
      await waitForVisible(page.locator('#prusa-form'));

      const syntheticToken = 'E2E-SYNTHETIC-TOKEN-VALUE';
      await page.locator('#prusa-token').fill(syntheticToken);
      await page.locator('#prusa-fingerprint').fill('aabbccddee02');
      await page.locator('#prusa-save').click();
      await waitForVisible(page.locator('#reauth-dialog[open]'));
      await page.locator('#reauth-cancel').click();
      await waitForText(page.locator('#prusa-form .form-status'), 'Re-authentication is required');

      await page.locator('#prusa-save').click();
      await completeReauth(page);
      await waitForText(page.locator('#prusa-form .form-status'), 'fingerprint');
      await waitForText(page.locator('#prusa-token-state'), 'Token configured');

      const html = await page.content();
      assert(!html.includes(syntheticToken), 'stored token must never be rendered');
      assertEqual(await page.locator('#prusa-token').inputValue(), '', 'token field cleared');
    },
  },
  {
    name: 'timelapses empty/gallery/filter/pagination/frame preview/download/range/build/playback',
    async run({ page, base, request }) {
      await login(page, base);
      await openView(page, 'Timelapses');
      await waitForVisible(page.locator('#timelapse-videos'));
      assertEqual(await page.locator('#timelapse-video-count').textContent(), '14', 'video count');
      assertEqual(await page.locator('#timelapse-frame-count').textContent(), '14', 'frame count');
      assertEqual(await page.locator('#timelapse-videos-body tr').count(), 10, 'first page size');

      await page.locator('[data-timelapse-filter="completed"]').click();
      await page.waitForTimeout(300);
      const completedRows = await page.locator('#timelapse-videos-body tr').count();
      assert(completedRows > 0 && completedRows < 10, 'completed filter narrows the gallery');
      await page.locator('[data-timelapse-filter="all"]').click();
      await page.waitForTimeout(300);

      await page.locator('#timelapse-videos-next').click();
      await waitForText(page.locator('#timelapse-videos-page'), 'Page 2', 5000);
      await page.locator('#timelapse-videos-prev').click();

      // Frame preview loads from the authenticated frame route.
      const preview = page.locator('#timelapse-frames-grid img').first();
      await waitForVisible(preview);
      const naturalWidth = await preview.evaluate((img) => img.naturalWidth);
      assert(naturalWidth > 0, 'frame preview decoded');

      // Direct download works.
      const [videoDownload] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('#timelapse-videos-body a').first().click(),
      ]);
      assert(videoDownload.suggestedFilename().endsWith('.avi'), 'video download filename');

      // A single byte range is honoured without buffering the whole file.
      const firstHref = await page.locator('#timelapse-videos-body a').first().getAttribute('href');
      const range = await page.evaluate(async (href) => {
        const response = await fetch(href, {
          headers: { Range: 'bytes=0-9' },
          credentials: 'same-origin',
        });
        return { status: response.status, contentRange: response.headers.get('Content-Range') };
      }, firstHref);
      assertEqual(range.status, 206, 'single range status');
      assertIncludes(range.contentRange, 'bytes 0-9/', 'single range Content-Range');

      // Build: progress -> complete.
      await page.locator('#timelapse-build').click();
      await waitForText(page.locator('#timelapse-build-status'), 'Build complete', 12000);
      assertEqual(await page.locator('#timelapse-build').isEnabled(), true, 'build re-enabled');

      // Build: empty device and error are honest errors.
      await setScenario(request, base, { build: 'empty' });
      await page.locator('#timelapse-build').click();
      await waitForText(page.locator('#timelapse-build-status'), 'no frames to assemble');
      await setScenario(request, base, { build: 'error' });
      await page.locator('#timelapse-build').click();
      await waitForText(page.locator('#timelapse-build-status'), 'Build failed');
      // Duplicate prevention: a busy build is refused and the UI follows the
      // pre-existing job instead of claiming a new build.
      await setScenario(request, base, { build: 'busy' });
      await page.locator('#timelapse-build').click();
      await waitForText(page.locator('#timelapse-build-status'), 'Building');
      assert((await counters(request, base)).build_busy >= 1, 'duplicate build was refused');

      // Playback: honest fallback when the browser cannot decode MJPEG AVI.
      const supported = await page.evaluate(() => {
        const probe = document.createElement('video');
        return Boolean(probe.canPlayType && probe.canPlayType('video/x-msvideo'));
      });
      if (!supported) {
        await page.locator('#timelapse-videos-body button').first().click();
        await waitForText(page.locator('#timelapse-build-status'), 'cannot play MJPEG AVI inline');
      }

      // Empty state after a reset with no media.
      await reset(request, base, { media: 'empty' });
      await login(page, base);
      await openView(page, 'Timelapses');
      await waitForVisible(page.locator('#timelapse-videos-empty'));
      await waitForVisible(page.locator('#timelapse-frames-empty'));
    },
  },
  {
    name: 'system/update check/install uncertainty/diagnostics/SSH/recovery/reboot/reset',
    async run({ page, base, request }) {
      await login(page, base);
      await openView(page, 'System');
      await waitForVisible(page.locator('#system-health-title'));
      await waitForText(page.locator('#system-app-version'), '1.2.3');
      assertIncludes(await page.locator('#system-provisioning').textContent(), 'claimed', 'provisioning');

      // No action fires on page render.
      const before = await counters(request, base);
      for (const name of ['reboot', 'ssh', 'recovery', 'reset', 'update_check', 'update_install']) {
        assertEqual(before[name], 0, `no ${name} action on render`);
      }

      await page.locator('#system-update-check').click();
      await waitForText(page.locator('#system-update-status'), 'Report-only check started');

      await page.locator('#system-update-install').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-update-status'), 'may disconnect');

      await page.locator('#system-diagnostics-load').click();
      await waitForVisible(page.locator('#system-diagnostics-output'));
      assertIncludes(
        await page.locator('#system-diagnostics-output').textContent(),
        'synthetic diagnostics',
        'diagnostics output',
      );

      await page.locator('#system-ssh-save').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-ssh-state'), 'SSH is');

      await page.locator('#system-recovery').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-ssh-state'), 'Recovery sentinel written');

      await page.locator('#system-reboot-confirm').check();
      await page.locator('#system-reboot').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-reboot-status'), 'Reboot accepted');

      await page.locator('#system-reboot-confirm').check();
      await page.locator('#system-reboot').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-reboot-status'), 'rate-limited', 10000);

      await page.locator('#system-reset-phrase').fill('RESET');
      assertEqual(await page.locator('#system-reset').isEnabled(), true, 'reset enabled by phrase');
      await page.locator('#system-reset').click();
      await completeReauth(page);
      await waitForText(page.locator('#system-reset-status'), 'Reset complete', 10000);

      const after = await harnessState(request, base);
      assert(after.counters.update_check >= 1 && after.counters.update_install >= 1, 'update actions recorded');
      assert(after.counters.ssh >= 1 && after.counters.reset === 1, 'fixed actions recorded');
      assertEqual(after.recovery_sentinel, true, 'recovery sentinel written by the fixed path');
      assert(after.counters.reboot >= 1, 'reboot action recorded');
    },
  },
  {
    name: 'network card: status, scan, static apply with re-auth, hostname and NTP',
    async run({ page, base, request }) {
      await login(page, base);
      await openView(page, 'System');
      await waitForText(page.locator('#network-current-ssid'), 'E2E-WiFi');
      assertIncludes(await page.locator('#network-current-address').textContent(), '192.0.2.10/24', 'address');
      assertIncludes(await page.locator('#network-current-time').textContent(), 'synchronized', 'clock state');
      assertEqual(await page.locator('#network-static').isVisible(), false, 'static fields hidden for DHCP');

      // Scan fills the datalist (the first poll starts the scan, the next returns it).
      await page.locator('#network-scan').click();
      await waitForText(page.locator('#network-scan-status'), '2 networks found', 10000);
      assertEqual(await page.locator('#network-ssid-list option').count(), 2, 'scan options');

      // Static: the fields appear and are validated client-side.
      await page.locator('input[name="network_ipv4"][value="manual"]').check();
      await waitForVisible(page.locator('#network-static'));
      await page.locator('#network-ssid').fill('E2E-WiFi');
      await page.locator('#network-address').fill('192.0.2.50');
      await page.locator('#network-prefix').fill('24');
      await page.locator('#network-gateway').fill('192.0.2.1');
      await page.locator('#network-dns').fill('192.0.2.1, 9.9.9.9');
      await page.locator('#network-psk').fill('e2e-wifi-password');
      const networkForm = page.locator('#network-form');
      await networkForm.getByRole('button', { name: 'Apply network settings' }).click();
      await waitForText(networkForm.locator('.form-status'), 'acknowledgement');
      assertEqual((await counters(request, base)).network_apply, 0, 'nothing sent without acknowledgement');

      await page.locator('#network-confirm').check();
      await networkForm.getByRole('button', { name: 'Apply network settings' }).click();
      await completeReauth(page);
      await waitForText(page.locator('#network-result'), 'new network settings are active', 25000);
      const after = await counters(request, base);
      assertEqual(after.network_apply, 1, 'one apply');
      assertEqual(after.network_psk_seen, 1, 'the password reached the server once');
      assertEqual(await page.locator('#network-psk').inputValue(), '', 'password field is cleared');
      assert(!(await page.content()).includes('e2e-wifi-password'), 'the password never appears in the page');
      assertIncludes(await page.locator('#network-current-address').textContent(), '192.0.2.50/24', 'new address shown');

      // Hostname: re-auth, then the reboot hint.
      await page.locator('#network-hostname').fill('cam-two');
      await page.locator('#network-hostname-form').getByRole('button', { name: 'Save hostname' }).click();
      await completeReauth(page);
      await waitForText(page.locator('#network-hostname-form .form-status'), 'Reboot the camera');
      assertEqual((await counters(request, base)).hostname, 1, 'hostname applied');

      // Time zone: the list comes from the device's tz database; saving needs no re-auth.
      await waitForCount(page.locator('#network-timezone option'), (n) => n > 20);
      assertEqual(await page.locator('#network-timezone').inputValue(), 'UTC', 'system zone preselected');
      await page.locator('#network-timezone').selectOption('Europe/Berlin');
      await page.locator('#network-timezone-form').getByRole('button', { name: 'Save time zone' }).click();
      await waitForText(page.locator('#network-timezone-form .form-status'), 'Saved.');
      assertEqual((await counters(request, base)).timezone, 1, 'time zone applied');
      await waitForText(page.locator('#network-current-time'), 'Europe/Berlin');

      // NTP: saved without re-auth.
      await page.locator('#network-ntp').fill('time.e2e.invalid, 192.0.2.1');
      await page.locator('#network-ntp-form').getByRole('button', { name: 'Save time servers' }).click();
      await waitForText(page.locator('#network-ntp-form .form-status'), 'Saved');
      assertEqual((await counters(request, base)).ntp, 1, 'ntp saved');
    },
  },
  {
    name: 'network card: a failed change reports the automatic revert; an old image says so',
    async run({ page, base, request }) {
      await setScenario(request, base, { network: 'revert' });
      await login(page, base);
      await openView(page, 'System');
      await waitForText(page.locator('#network-current-ssid'), 'E2E-WiFi');
      await page.locator('#network-ssid').fill('Other-Net');
      await page.locator('#network-psk').fill('another-password');
      await page.locator('#network-confirm').check();
      await page.locator('#network-form').getByRole('button', { name: 'Apply network settings' }).click();
      await completeReauth(page);
      await waitForText(page.locator('#network-result'), 'returned to the previous settings', 25000);
      assertIncludes(await page.locator('#network-result').textContent(), 'gateway unreachable', 'reason shown');
      assertIncludes(await page.locator('#network-current-ssid').textContent(), 'E2E-WiFi', 'old network kept');

      await setScenario(request, base, { network: 'old_image' });
      await page.locator('#network-psk').fill('another-password');
      await page.locator('#network-form').getByRole('button', { name: 'Apply network settings' }).click();
      await completeReauth(page);
      await waitForText(page.locator('#network-form .form-status'), 'needs a newer camera image');
    },
  },
  {
    name: 'timelapse GPIO trigger: pins, wiring panel, validation, live status and errors',
    async run({ page, base, request }) {
      await login(page, base);
      await openView(page, 'Camera');
      const form = page.locator('#timelapse-gpio-form');
      await waitForVisible(form);
      assertEqual(await page.locator('#timelapse-gpio-fields').isVisible(), false, 'pin fields hidden for the timer');
      assertEqual(await page.locator('#timelapse-interval-form').isVisible(), true, 'interval field shown for the timer');

      await page.locator('#timelapse-trigger').selectOption('gpio');
      await waitForVisible(page.locator('#timelapse-gpio-fields'));
      assertEqual(await page.locator('#timelapse-interval-form').isVisible(), false, 'interval field hidden for GPIO');
      // The dropdown offers the safe pins with header numbers and a ground pin.
      const labels = await page.locator('#timelapse-gpio-pin option').allTextContents();
      assert(labels.includes('GPIO17 — header pin 11 (GND: pin 9)'), 'GPIO17 label');
      assert(!labels.some((label) => /^GPIO(0|1|2|3|7|8|9|10|11|14|15) /.test(label)), 'unsafe pins are not offered');
      assertEqual(await page.locator('#timelapse-gpio-pin').inputValue(), '17', 'default layer pin');
      assertEqual(await page.locator('#timelapse-gpio-record-pin').inputValue(), '', 'recording pin defaults to none');

      // The same pin twice is refused before anything is sent.
      await page.locator('#timelapse-gpio-record-pin').selectOption('17');
      const settingsBefore = (await counters(request, base)).settings;
      await form.getByRole('button', { name: 'Save trigger' }).click();
      await waitForText(form.locator('.form-status'), 'must differ');
      assertEqual((await counters(request, base)).settings, settingsBefore, 'nothing sent for a conflicting pin pair');

      // Wiring panel follows the chosen pins and shows the recording G-code.
      await page.locator('#timelapse-gpio-pin').selectOption('22');
      await page.locator('#timelapse-gpio-record-pin').selectOption('27');
      await page.locator('#gpio-help summary').click();
      assertEqual(await page.locator('[data-gpio="shot-header"]').first().textContent(), '15', 'layer header pin');
      assertEqual(await page.locator('[data-gpio="ground-header"]').first().textContent(), '14', 'ground header pin');
      assertEqual(await page.locator('[data-gpio="record-header"]').first().textContent(), '13', 'record header pin');
      assertEqual(await page.locator('#gpio-help-record-start').isVisible(), true, 'recording G-code shown');
      assertIncludes(await page.locator('#gpio-help').textContent(), 'M264 P0 B1', 'layer pulse G-code');
      assertIncludes(await page.locator('#gpio-help').textContent(), 'G4 P100', 'explicit 100 ms pulse');

      await form.getByRole('button', { name: 'Save trigger' }).click();
      await waitForText(form.locator('.form-status'), 'Saved.');
      await waitForText(page.locator('#timelapse-gpio-status'), 'Armed on GPIO22', 12000);
      assertIncludes(await page.locator('#timelapse-gpio-status').textContent(), 'recording on GPIO27', 'record pin in status');
      assertIncludes(await page.locator('#timelapse-gpio-status').textContent(), 'pulse to frame 4.2 s', 'latency shown');
      // dwell = ceil(4.2 s) + 2 s margin
      assertEqual(await page.locator('[data-gpio="dwell"]').first().textContent(), '7', 'dwell from the measured latency');

      // A permission problem is shown, not swallowed.
      await setScenario(request, base, { gpio: 'error' });
      await waitForVisible(page.locator('#timelapse-gpio-error'), 12000);
      assertIncludes(await page.locator('#timelapse-gpio-error').textContent(), 'permission denied', 'gpio error');

      // Back to the timer: the interval field returns.
      await setScenario(request, base, { gpio: 'ok' });
      await page.locator('#timelapse-trigger').selectOption('interval');
      await form.getByRole('button', { name: 'Save trigger' }).click();
      await waitForText(form.locator('.form-status'), 'Saved.');
      await waitForVisible(page.locator('#timelapse-interval-form'));
    },
  },
  {
    name: 'timelapses: print sessions, session build and the clock warning',
    async run({ page, base, request }) {
      await setScenario(request, base, { clock: 'unsynced' });
      await login(page, base);
      await openView(page, 'Timelapses');
      await waitForVisible(page.locator('#timelapse-clock-warning'));
      const options = await page.locator('#timelapse-session option').allTextContents();
      assertEqual(options.length, 3, 'loose frames plus two sessions');
      assertIncludes(options[1], 'session_20260102-000000', 'newest session first');
      assertIncludes(options[2], '3 frames', 'frame count in the picker');

      await page.locator('#timelapse-session').selectOption('session_20260101-000000');
      await waitForCount(page.locator('#timelapse-frames-grid figure'), (n) => n === 3);
      const src = await page.locator('#timelapse-frames-grid img').first().getAttribute('src');
      assertIncludes(src, 'session=session_20260101-000000', 'preview URL carries the session');

      await page.locator('#timelapse-build').click();
      await waitForText(page.locator('#timelapse-build-status'), 'Build complete', 12000);
      assertEqual((await counters(request, base)).build_session, 1, 'the session was built');

      // Loose frames offer no delete; a session does, behind an acknowledgement and re-auth.
      await page.locator('#timelapse-session').selectOption('');
      await waitForCount(page.locator('#timelapse-frames-grid figure'), (n) => n === 12);
      assertEqual(await page.locator('#timelapse-session-delete').isVisible(), false, 'no delete for loose frames');

      await page.locator('#timelapse-session').selectOption('session_20260101-000000');
      await waitForVisible(page.locator('#timelapse-session-delete'));
      await page.locator('#timelapse-session-delete-button').click();
      await waitForText(page.locator('#timelapse-session-delete-status'), 'acknowledgement');
      assertEqual(await page.locator('#timelapse-session option').count(), 3, 'nothing deleted without acknowledgement');

      await page.locator('#timelapse-session-delete-confirm').check();
      await page.locator('#timelapse-session-delete-button').click();
      await completeReauth(page);
      await waitForText(page.locator('#timelapse-session-delete-status'), 'Deleted session_20260101-000000: 3 frames');
      await waitForCount(page.locator('#timelapse-session option'), (n) => n === 2);
      assertEqual(await page.locator('#timelapse-session').inputValue(), '', 'selection falls back to loose frames');
      await waitForCount(page.locator('#timelapse-frames-grid figure'), (n) => n === 12);
      assertEqual(await page.locator('#timelapse-session-delete').isVisible(), false, 'delete hidden again');

      // Synchronized clock: the warning disappears on the next visit.
      await setScenario(request, base, { clock: 'synced' });
      await openView(page, 'Overview');
      await openView(page, 'Timelapses');
      await page.locator('#timelapse-clock-warning').waitFor({ state: 'hidden' });
    },
  },
  {
    name: 'keyboard/focus/ARIA/overview and no horizontal overflow at 360px',
    async run({ page, base, viewport }) {
      // Keyboard: the login view autofocuses the password; Tab reaches submit.
      await page.goto(`${base}/admin`);
      await waitForVisible(page.locator('#login-view'));
      assertEqual(
        await page.evaluate(() => document.activeElement.id), 'password',
        'initial password focus',
      );
      await page.keyboard.press('Tab');
      assertEqual(
        await page.evaluate(() => document.activeElement.id), 'login-submit',
        'tab to sign-in',
      );
      await page.keyboard.press('Shift+Tab');
      assertEqual(
        await page.evaluate(() => document.activeElement.id), 'password',
        'shift-tab back',
      );
      assertEqual(await page.locator('label[for="password"]').count(), 1, 'label association');
      await page.locator('#password').fill(PASSWORD);
      await page.getByRole('button', { name: 'Sign in' }).click();
      await waitForVisible(page.locator('#app-view'));

      // Keyboard focus must be visible on a nav control.
      await page.locator('.nav__item[data-view="camera"]').focus();
      await page.keyboard.press('Tab');
      const outlineWidth = await page.evaluate(() => {
        const el = document.activeElement;
        return getComputedStyle(el).outlineWidth;
      });
      assert(outlineWidth && outlineWidth !== '0px', `focus ring visible (got ${outlineWidth})`);

      // Touch-target intent on the primary navigation.
      for (const item of await page.locator('.nav__item').all()) {
        const box = await item.boundingBox();
        assert(box && box.height >= 44, `nav touch target >= 44px (got ${box && box.height})`);
      }

      // No horizontal overflow on any view at the current viewport.
      for (const name of ['Overview', 'Camera', 'Timelapses', 'Integrations', 'System']) {
        await openView(page, name);
        await page.waitForTimeout(250);
        const overflow = await page.evaluate(() => (
          document.documentElement.scrollWidth - document.documentElement.clientWidth
        ));
        assert(overflow <= 1, `no horizontal overflow on ${name} (overflow=${overflow})`);
      }

      // Reduced motion neutralizes the spinner animation.
      await page.emulateMedia({ reducedMotion: 'reduce' });
      const spinnerAnimation = await page.evaluate(() => (
        getComputedStyle(document.querySelector('#boot .spinner')).animationName
      ));
      assertEqual(spinnerAnimation, 'none', 'reduced-motion spinner');
      assert(viewport.width <= 400 || viewport.width >= 1000, 'known viewport');
    },
  },
  {
    name: 'setup wizard: complete onboarding from the captive portal',
    async run({ page, base, request }) {
      await reset(request, base, { mode: 'setup' });
      // An unknown portal path (an OS connectivity probe) lands on the wizard.
      await page.goto(`${base}/generate_204`);
      await setupScreen(page, 'welcome');
      assertIncludes(page.url(), '/setup', 'probe path redirected to the wizard');
      assertEqual(await page.locator('#setup-ssid').textContent(), 'PiBuddyCam-Setup-2e0001', 'setup ssid');
      await waitForText(page.locator('#status-camera'), 'Detected');
      await page.locator('#welcome-next').click();
      await setupScreen(page, 'wifi');

      // The scan runs on entry; picking a network fills the SSID.
      await waitForText(page.locator('#wifi-networks'), 'E2E-Home');
      await page.locator('.network-list__item', { hasText: 'E2E-Home' }).click();
      assertEqual(await page.locator('#wifi-ssid').inputValue(), 'E2E-Home', 'picked ssid');
      await page.locator('#wifi-psk').fill('short');
      await page.locator('#wifi-next').click();
      await waitForText(page.locator('#wifi-error'), '8 to 63');
      await page.locator('#wifi-psk').fill('synthetic-wifi-psk');
      await page.locator('#wifi-next').click();

      await setupScreen(page, 'prusa');
      await page.locator('#prusa-next').click();
      await waitForText(page.locator('#prusa-error'), 'Paste the camera token');
      await page.locator('#prusa-token').fill('synthetic-registration-token');
      await page.locator('#prusa-next').click();

      await setupScreen(page, 'password');
      await page.locator('#admin-password').fill('synthetic-admin-pass');
      await page.locator('#admin-password-confirm').fill('something-else');
      await page.locator('#password-next').click();
      await waitForText(page.locator('#password-error'), 'do not match');
      await page.locator('#admin-password-confirm').fill('synthetic-admin-pass');
      await page.locator('#password-next').click();

      await setupScreen(page, 'options');
      await page.locator('#mqtt-enabled').check();
      await page.locator('#mqtt-uri').fill('mqtt://broker.e2e.invalid:1883');
      await page.locator('#mqtt-test').click();
      await waitForText(page.locator('#mqtt-test-status'), 'Connected to the broker');
      await page.locator('#options-next').click();

      await setupScreen(page, 'review');
      const review = await page.locator('#review-list').textContent();
      assertIncludes(review, 'E2E-Home (password saved)', 'review wifi');
      assertIncludes(review, 'mqtt://broker.e2e.invalid:1883', 'review mqtt');
      assertIncludes(review, 'https://pibuddycam-2e0001.local', 'review console address');
      for (const secret of ['synthetic-wifi-psk', 'synthetic-registration-token', 'synthetic-admin-pass']) {
        assert(!(await page.content()).includes(secret), `secret ${secret} must not be rendered`);
      }
      const overflow = await page.evaluate(() => (
        document.documentElement.scrollWidth - document.documentElement.clientWidth
      ));
      assert(overflow <= 1, `no horizontal overflow on review (overflow=${overflow})`);

      await page.locator('#review-finish').click();
      await setupScreen(page, 'done');
      assertEqual(await page.locator('#done-url').textContent(), 'https://pibuddycam-2e0001.local', 'done url');
      assertEqual(await page.locator('#done-ssid').textContent(), 'E2E-Home', 'done ssid');
      const done = await counters(request, base);
      assertEqual(done.setup_station, 1, 'station activated once');
      assertEqual(done.setup_camera, 1, 'camera started once');

    },
  },
  {
    name: 'setup wizard: manual network, resume after reload, station failure',
    async run({ page, base, request }) {
      await reset(request, base, {
        mode: 'setup', scenarios: { setup_scan: 'fail', setup_station: 'fail' },
      });
      await setupToWifi(page, base);
      await waitForText(page.locator('#wifi-scan-status'), 'Type the network name');
      await page.locator('#wifi-ssid').fill('E2E-Hidden');
      await page.locator('#wifi-psk').fill('synthetic-wifi-psk');
      await page.locator('#wifi-next').click();
      await setupScreen(page, 'prusa');

      // A reload resumes at the first incomplete screen; nothing is re-asked.
      await page.reload();
      await setupScreen(page, 'prusa');
      await page.locator('#prusa-token').fill('synthetic-registration-token');
      await page.locator('#prusa-next').click();
      await setupScreen(page, 'password');
      await page.locator('#admin-password').fill('synthetic-admin-pass');
      await page.locator('#admin-password-confirm').fill('synthetic-admin-pass');
      await page.locator('#password-next').click();
      await setupScreen(page, 'options');
      await page.locator('#options-next').click();
      await setupScreen(page, 'review');

      // Going back keeps the saved Wi-Fi password when the field stays empty.
      await page.locator('#review-list button[aria-label="Change Wi-Fi network"]').click();
      await setupScreen(page, 'wifi');
      await waitForText(page.locator('#wifi-psk-hint'), 'A password is saved');
      await page.locator('#wifi-next').click();
      await setupScreen(page, 'prusa');
      await page.locator('#prusa-next').click();
      await setupScreen(page, 'password');
      await page.locator('#password-next').click();
      await setupScreen(page, 'options');
      await page.locator('#options-next').click();
      await setupScreen(page, 'review');
      assertIncludes(await page.locator('#review-list').textContent(), 'E2E-Hidden (password saved)', 'psk kept');

      await page.locator('#review-finish').click();
      await setupScreen(page, 'review');
      await waitForText(page.locator('#review-error'), 'network not found');
      assertEqual((await counters(request, base)).setup_camera, 0, 'camera not started');
    },
  },
  {
    name: 'no severe console or page errors across the console',
    async run({ page, base }) {
      await login(page, base);
      for (const name of ['Camera', 'Timelapses', 'Integrations', 'System', 'Overview']) {
        await openView(page, name);
        await page.waitForTimeout(250);
      }
      await waitForVisible(page.locator('#live-frame'));
      // The runner's global collector asserts there were no severe errors.
    },
  },
];

/* ------------------------------------------------------------------ */
/* runner                                                              */
/* ------------------------------------------------------------------ */
const SEVERE_NETWORK = /Failed to load resource|status of [45]\d\d/i;

function isSevereConsole(text) {
  return !SEVERE_NETWORK.test(text);
}

async function main() {
  const playwright = await loadPlaywright();
  if (!playwright) {
    console.log('E2E SKIP: the Playwright library is not installed locally.');
    process.exit(process.env.E2E_REQUIRED === '1' ? 1 : 0);
  }

  let server;
  try {
    server = await startServer();
  } catch (error) {
    console.error(`E2E FAIL: ${error.message}`);
    process.exit(1);
  }
  const { child, base } = server;
  installCleanup(child);
  console.log(`E2E server: ${base}`);

  let browser;
  try {
    browser = await playwright.chromium.launch({ headless: !headed });
  } catch (error) {
    await stopServer(child);
    console.log(`E2E SKIP: Chromium is unavailable (${error.message.split('\n')[0]}).`);
    process.exit(process.env.E2E_REQUIRED === '1' ? 1 : 0);
  }

  const viewports = [
    { id: 'desktop', width: 1280, height: 900 },
    { id: 'mobile', width: 360, height: 800 },
  ];
  const selected = grep ? TESTS.filter((t) => t.name.includes(grep)) : TESTS;

  let passed = 0;
  const failures = [];
  for (const test of selected) {
    for (const viewport of viewports) {
      const label = `${test.name} [${viewport.id}]`;
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        acceptDownloads: true,
      });
      await context.tracing.start({ screenshots: true, snapshots: true });
      const page = await context.newPage();
      const request = context.request;
      const severeErrors = [];
      page.on('pageerror', (error) => severeErrors.push(`pageerror: ${error.message}`));
      page.on('console', (message) => {
        if (message.type() === 'error' && isSevereConsole(message.text())) {
          severeErrors.push(`console: ${message.text()}`);
        }
      });
      page.on('dialog', (dialog) => dialog.accept());
      try {
        await reset(request, base);
        await test.run({ page, base, request, viewport });
        assertEqual(severeErrors.length, 0, `severe browser errors: ${severeErrors.join(' | ')}`);
        passed += 1;
        console.log(`ok   ${label}`);
        await context.tracing.stop().catch(() => {});
      } catch (error) {
        failures.push({ label, message: error.message });
        console.log(`FAIL ${label}: ${error.message}`);
        // Diagnostics only on failure; the fixture is synthetic, so the trace
        // and screenshot contain no real credential or identifier.
        try {
          fs.mkdirSync(ARTIFACTS, { recursive: true });
          const slug = label.replace(/[^a-z0-9]+/gi, '-').toLowerCase();
          await page.screenshot({ path: path.join(ARTIFACTS, `${slug}.png`) }).catch(() => {});
          await context.tracing
            .stop({ path: path.join(ARTIFACTS, `${slug}.zip`) })
            .catch(() => {});
          console.log(`     artifacts: ${path.relative(REPO_ROOT, ARTIFACTS)}/${slug}.*`);
        } catch (_artifactError) {
          /* diagnostics must never mask the failure */
        }
      } finally {
        await context.close();
      }
    }
  }

  try {
    await browser.close();
  } finally {
    await stopServer(child);
  }

  console.log('');
  console.log(`E2E ${passed} passed, ${failures.length} failed (desktop+mobile).`);
  if (failures.length) {
    for (const failure of failures) {
      console.log(`  - ${failure.label}: ${failure.message}`);
    }
    process.exit(1);
  }
  if (selected.length === 0) {
    console.log('E2E FAIL: no tests matched --grep.');
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(`E2E FAIL: ${error.stack || error.message}`);
  process.exit(1);
});

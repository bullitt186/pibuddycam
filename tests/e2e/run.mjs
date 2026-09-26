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
      assertEqual(download.suggestedFilename(), 'buddy3d-snapshot.jpg', 'snapshot filename');

      await setScenario(request, base, { live: 'stale' });
      await waitForText(page.locator('#live-placeholder'), 'stale', 10000);
      assertIncludes(await page.locator('#live-state').textContent(), 'Stale', 'stale chip');
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

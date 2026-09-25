/*
 * Buddy3D Camera local console — front-end foundation.
 *
 * No framework, no CDN, no build step. The module talks to the existing
 * authenticated admin API only:
 *
 *   GET  /api/session  -> { ok, mode, csrf }  (session probe / expiry)
 *   POST /api/login    -> { ok, mode, csrf }  (sets the HttpOnly cookie)
 *   POST /api/logout   -> { ok }              (requires the CSRF header)
 *
 * The CSRF token is held in a module-scoped variable (memory only): it is
 * never written to localStorage, sessionStorage, a cookie, or the DOM. Every
 * authenticated request returns the login view on HTTP 401, which is the
 * session-expiry contract. No device data is fetched or rendered here.
 */

const SESSION_STATE = {
  csrf: null,
  mode: null,
};

const els = {};

function cacheElements() {
  els.boot = document.getElementById('boot');
  els.main = document.getElementById('main');
  els.loginView = document.getElementById('login-view');
  els.appView = document.getElementById('app-view');
  els.loginForm = document.getElementById('login-form');
  els.password = document.getElementById('password');
  els.loginSubmit = document.getElementById('login-submit');
  els.loginError = document.getElementById('login-error');
  els.logout = document.getElementById('logout');
  els.sessionState = document.getElementById('session-state');
  els.navItems = Array.from(document.querySelectorAll('.nav__item'));
  els.panels = Array.from(document.querySelectorAll('[data-view-panel]'));
}

function showBoot() {
  if (els.boot) els.boot.hidden = false;
  if (els.main) els.main.hidden = true;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = true;
}

function showLogin(message) {
  if (els.boot) els.boot.hidden = true;
  if (els.main) els.main.hidden = false;
  if (els.loginView) els.loginView.hidden = false;
  if (els.appView) els.appView.hidden = true;
  setLoginError(message || '');
  if (els.password) els.password.focus();
}

function showApp(mode) {
  SESSION_STATE.mode = mode || null;
  if (els.boot) els.boot.hidden = true;
  if (els.main) els.main.hidden = false;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = false;
  if (els.sessionState) {
    els.sessionState.textContent = mode ? `Signed in · ${mode}` : 'Signed in';
  }
  selectView('overview');
}

function setLoginError(message) {
  if (!els.loginError) return;
  els.loginError.textContent = message;
  els.loginError.hidden = !message;
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  } else {
    button.textContent = button.dataset.label || label;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

/**
 * Perform one same-origin JSON request. Never throws: a network failure is
 * reported as status 0 so callers can show an honest error state.
 */
async function request(path, options = {}) {
  const headers = { Accept: 'application/json' };
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    headers,
  };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  if (options.csrf) {
    headers['X-CSRF-Token'] = SESSION_STATE.csrf || '';
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { ...init, signal: controller.signal });
    let data = null;
    const text = await response.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_error) {
        data = null;
      }
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      retryAfter: response.headers.get('Retry-After'),
    };
  } catch (_error) {
    return { ok: false, status: 0, data: null, retryAfter: null };
  } finally {
    clearTimeout(timer);
  }
}

function clearSession() {
  SESSION_STATE.csrf = null;
  SESSION_STATE.mode = null;
}

/** The session-expiry path: any authenticated 401 returns to login. */
function handleExpired() {
  clearSession();
  showLogin('Your session expired. Sign in again.');
}

function selectView(name) {
  els.navItems.forEach((item) => {
    const current = item.dataset.view === name;
    if (current) {
      item.setAttribute('aria-current', 'page');
    } else {
      item.removeAttribute('aria-current');
    }
  });
  els.panels.forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== name;
  });
}

function wireNavigation() {
  els.navItems.forEach((item) => {
    item.addEventListener('click', () => selectView(item.dataset.view));
  });
}

async function probeSession() {
  const result = await request('/api/session');
  if (result.ok && result.data && result.data.ok) {
    SESSION_STATE.csrf = result.data.csrf || null;
    showApp(result.data.mode);
    return;
  }
  showLogin();
}

async function submitLogin(event) {
  event.preventDefault();
  const password = els.password ? els.password.value : '';
  if (!password) {
    setLoginError('Enter the admin password.');
    return;
  }
  setLoginError('');
  setBusy(els.loginSubmit, true, 'Signing in…');
  const result = await request('/api/login', {
    method: 'POST',
    body: { password },
  });
  if (els.password) els.password.value = '';
  setBusy(els.loginSubmit, false, 'Sign in');

  if (result.ok && result.data && result.data.ok) {
    SESSION_STATE.csrf = result.data.csrf || null;
    showApp(result.data.mode);
    return;
  }
  if (result.status === 429) {
    const wait = result.retryAfter ? `${result.retryAfter}s` : 'a minute';
    setLoginError(`Too many attempts. Try again in ${wait}.`);
    return;
  }
  if (result.status === 401) {
    setLoginError('Incorrect password.');
    return;
  }
  setLoginError('Sign-in failed. Check the connection and try again.');
}

async function submitLogout() {
  setBusy(els.logout, true, 'Signing out…');
  const result = await request('/api/logout', { method: 'POST', csrf: true });
  setBusy(els.logout, false, 'Sign out');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  clearSession();
  showLogin();
}

function wireForms() {
  if (els.loginForm) {
    els.loginForm.addEventListener('submit', submitLogin);
  }
  if (els.logout) {
    els.logout.addEventListener('click', submitLogout);
  }
}

function init() {
  cacheElements();
  wireNavigation();
  wireForms();
  showBoot();
  probeSession();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

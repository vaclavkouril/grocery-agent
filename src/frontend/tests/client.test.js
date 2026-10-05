// Offline contract tests for the independent website application.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
import {randomUUID} from 'node:crypto';
import {ApiError, GroceryClient, recipePayload, decimal} from '../assets/client.js';
import {language, dictionaries, t, errorMessage, staticTranslations, displayDecimal, sourceKey} from '../assets/i18n.js';
import {AccountUI, fieldLabels} from '../assets/account.js';
import {clearValidation, showValidation} from '../assets/validation.js';

const capabilities = {providers: ['template'], cache_policies: ['cache-only'], ingredients: ['rice', 'oil'], sources: ['kupi']};
const form = {provider: 'template', model: '', cache_policy: 'cache-only', meal_style: 'main', servings: '2', max_stores: '1', budget: '12.3400', exclusions: []};

test('registration sends only credentials and selected session mode, keeps bearer session in memory', async () => {
  const calls = [];
  const client = new GroceryClient(async (path, options) => {
    calls.push([path, options]);
    return new Response(JSON.stringify({token: 'registered-session', session_mode: 'bearer'}));
  });
  client.setSession({token: 'old-session', csrf_token: 'old-csrf', session_mode: 'cookie'});
  await client.register('new-user', 'fixture private password');
  assert.equal(calls[0][0], '/v1/auth/register');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {username: 'new-user', password: 'fixture private password', session_mode: 'bearer'});
  assert.equal(calls[0][1].headers.get('Authorization'), null);
  assert.equal(calls[0][1].headers.get('X-CSRF-Token'), null);
  assert.equal(calls[0][1].credentials, 'omit');
  assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(calls[0][1].redirect, 'error');
  assert.equal(client.token, 'registered-session');
  assert.equal(client.cookieMode, false);
  assert.equal(client.csrf, '');
  assert.equal(JSON.stringify(client).includes('fixture private password'), false);
});

test('registration cookie sessions support CSRF, resume, logout and subsequent password login', async () => {
  const calls = [];
  const client = new GroceryClient(async (path, options) => {
    calls.push([path, options]);
    if (path === '/v1/session') return new Response(null, {status: 204});
    return new Response(JSON.stringify({session_mode: 'cookie', csrf_token: 'registered-csrf'}));
  });
  await client.register('alice', 'private password', 'cookie');
  await client.submit({provider: 'template'}, 'key');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(calls[0][1].headers.get('X-CSRF-Token'), '');
  assert.equal(calls[1][1].headers.get('X-CSRF-Token'), 'registered-csrf');
  assert.equal(calls[1][1].headers.get('Authorization'), null);
  await client.resume(); await client.logout();
  assert.equal(calls[2][0], '/v1/auth/session');
  assert.equal(calls[3][1].headers.get('X-CSRF-Token'), 'registered-csrf');
  client.clearSession();
  await client.login('alice', 'private password', 'cookie');
  assert.equal(calls[4][0], '/v1/auth/login');
  assert.equal(calls[4][1].headers.get('Authorization'), null);
});

for (const status of [401, 409, 422, 429]) test(`registration failure ${status} leaves no session credentials`, async () => {
  const client = new GroceryClient(async () => new Response(JSON.stringify({detail: 'Registration failed'}), {status}));
  client.setSession({token: 'old', csrf_token: 'old', session_mode: 'cookie'});
  await assert.rejects(client.register('alice', 'private password', 'cookie'), error => error instanceof ApiError && error.status === status);
  assert.equal(client.token, ''); assert.equal(client.csrf, ''); assert.equal(client.cookieMode, false);
});

test('registration network errors clear session state too', async () => {
  const client = new GroceryClient(async () => { throw new TypeError('Offline'); });
  await assert.rejects(client.register('alice', 'private password', 'cookie'), /Offline/);
  assert.equal(client.cookieMode, false); assert.equal(client.token, '');
});

// Run the actual browser event handlers against a small DOM stand-in; no external services.
async function browserApp(auth = {registration: true, password_login: true}, registerResponse, responses = {}) {
  const [source, html] = await Promise.all([
    readFile(new URL('../assets/app.js', import.meta.url), 'utf8'),
    readFile(new URL('../index.html', import.meta.url), 'utf8'),
  ]);
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
  ids.push('auth-tabs', 'auth-login-tab', 'auth-register-tab', 'signin-heading', 'signin-description', 'register-form', 'register-username', 'register-password', 'register-password-confirm', 'register-error');
  const elements = new Map();
  const document = {
    documentElement: {lang: 'en', childNodes: [], getAttribute() { return null; }},
    activeElement: null,
    getElementById: id => elements.get(id),
    createElement: tag => element(tag),
    querySelectorAll: selector => [...elements.values()].filter(item => selector === '[data-i18n]' ? item.dataset.i18n : /password|username|token|auth-.*tab/.test(item.id)),
  };
  function element(id) {
    const listeners = {}, attributes = {};
    return {
      id, tagName: id.toUpperCase(), value: '', hidden: false, disabled: false, children: [], textContent: '', dataset: {},
      addEventListener(type, handler) { listeners[type] = handler; },
      dispatch(type, properties = {}) { listeners[type]?.({currentTarget: this, preventDefault() {}, ...properties}); },
      setAttribute(name, value) { attributes[name] = value; },
      getAttribute(name) { return attributes[name] ?? null; },
      removeAttribute(name) { delete attributes[name]; },
      replaceChildren(...children) { this.children = children; if (children[0]?.value !== undefined) this.value = children[0].value; },
      append(...children) { this.children.push(...children); },
      after(item) { this.following = item; },
      closest() { return null; }, reset() {}, querySelectorAll() { return []; }, remove() {},
      focus() { document.activeElement = this; },
      get selectedOptions() { return this.children.filter(child => child.value === this.value); },
      get childNodes() { return this.children; },
      querySelector(selector) {
        if (selector === 'select' || selector === 'input') {
          const tagName = selector.toUpperCase();
          const find = children => {
            for (const child of children) {
              if (child.tagName === tagName) return child;
              const descendant = find(child.children ?? []);
              if (descendant) return descendant;
            }
            return null;
          };
          return find(this.children);
        }
        return this.submitButton ?? null;
      },
    };
  }
  for (const id of ids) elements.set(id, element(id));
  const tokenAccess = element('token-signin-details'); tokenAccess.open = true;
  elements.get('signin-form').closest = () => tokenAccess;
  for (const id of ['signin-form', 'password-form', 'invite-form', 'register-form']) {
    const button = element('submit button');
    button.replaceChildren({textContent: id === 'register-form' ? 'Create account' : 'Sign in'});
    elements.get(id).submitButton = button;
  }
  const calls = [], windowListeners = {};
  const supported = {...capabilities, ingredient_labels: {}, models: {}, meal_styles: ['main'], default_provider: 'template', default_cache_policy: 'cache-only', recipe_request_schema: {properties: {servings: {minimum: 1, maximum: 20, default: 2}, max_stores: {minimum: 1, maximum: 3, default: 1}}}, offer_query_schema: {$defs: {Unit: {enum: ['kg']}}}};
  const fetcher = async (path, options) => {
    calls.push([path, options]);
    if (Object.hasOwn(responses, path)) return typeof responses[path] === 'function' ? responses[path](path, options) : new Response(JSON.stringify(responses[path]));
    if (path === '/v1/auth/register') return registerResponse ? registerResponse(path, options) : new Response(JSON.stringify({token: 'new-session', session_mode: 'bearer'}));
    const bodies = {'/v1/auth/capabilities': auth, '/v1/me': {username: 'alice', role: 'user'}, '/v1/capabilities': supported, '/v1/jobs?offset=0&limit=10': {items: [], total: 0, offset: 0, limit: 10}, '/v1/collections?source=kupi': []};
    return new Response(JSON.stringify(bodies[path] ?? {}));
  };
  let client;
  class BrowserClient extends GroceryClient { constructor() { super(fetcher); client = this; } }
  const storage = new Proxy({}, {get() { throw new Error('Identity must never access browser storage'); }, set() { throw new Error('Identity must never persist in browser storage'); }});
  language.current = 'en'; language.explicit = null; language.browser = 'en';
  runInNewContext(source.replace(/^import .*;$/gm, ''), {
    GroceryClient: BrowserClient, ApiError, recipePayload, decimal, language, dictionaries, t, errorMessage, staticTranslations, displayDecimal, sourceKey, AccountUI, fieldLabels, clearValidation, showValidation,
    choiceList: () => ({render() {}, destroy() {}}), retailerChoices: () => ({render() {}, destroy() {}}), document, Event,
    window: {addEventListener(type, handler) { windowListeners[type] = handler; }},
    localStorage: storage, sessionStorage: storage, clearTimeout, setTimeout, crypto: {randomUUID},
  });
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); };
  await settle();
  return {get: id => elements.get(id), document, calls, client, settle, tokenAccess, pagehide: () => windowListeners.pagehide()};
}

for (const registration of [false, undefined, 'true']) test(`registration tabs require explicit permission (${registration})`, async () => {
  const app = await browserApp({registration, password_login: false});
  assert.equal(app.get('auth-tabs').hidden, true);
  assert.equal(app.get('register-form').hidden, true);
  assert.equal(app.get('signin-form').hidden, false);
  assert.equal(app.get('invite-form').hidden, false);
  assert.equal(app.get('password-form').hidden, true);
  assert.equal(app.tokenAccess.open, true);
  assert.equal(app.tokenAccess.hidden, false);
  app.get('register-form').dispatch('submit'); await app.settle();
  assert.equal(app.calls.some(([path]) => path === '/v1/auth/register'), false);
});

test('registration tabs update aria selections, form visibility and keyboard focus', async () => {
  const app = await browserApp();
  assert.equal(app.get('auth-tabs').hidden, false);
  assert.equal(app.tokenAccess.open, false);
  app.get('auth-register-tab').dispatch('click');
  assert.equal(app.get('register-form').hidden, false);
  assert.equal(app.get('password-form').hidden, true);
  assert.equal(app.get('signin-form').hidden, true);
  assert.equal(app.get('invite-form').hidden, true);
  assert.equal(app.tokenAccess.hidden, true);
  assert.equal(app.get('auth-register-tab').getAttribute('aria-selected'), 'true');
  assert.equal(app.get('auth-login-tab').tabIndex, -1);
  assert.equal(app.document.activeElement.id, 'register-username');
  app.get('auth-register-tab').dispatch('keydown', {key: 'ArrowLeft'});
  assert.equal(app.get('register-form').hidden, true);
  assert.equal(app.get('password-form').hidden, false);
  assert.equal(app.tokenAccess.hidden, false);
  assert.equal(app.tokenAccess.open, false);
  assert.equal(app.document.activeElement.id, 'auth-login-tab');
});

test('confirmation mismatch stays inline, clears passwords and sends no request', async () => {
  const app = await browserApp();
  app.get('auth-register-tab').dispatch('click');
  app.get('register-password').value = 'first password';
  app.get('register-password-confirm').value = 'second password';
  app.get('register-form').dispatch('submit'); await app.settle();
  assert.match(app.get('register-error').textContent, /Passwords do not match/);
  assert.equal(app.get('register-error').hidden, false);
  assert.equal(app.get('register-password-confirm').getAttribute('aria-invalid'), 'true');
  assert.equal(app.get('register-password').value, '');
  assert.equal(app.get('register-password-confirm').value, '');
  assert.equal(app.document.activeElement.id, 'register-password');
  assert.equal(app.calls.some(([path]) => path === '/v1/auth/register'), false);
});

test('register 401 is inline, duplicate submissions are guarded and controls recover', async () => {
  let finish;
  const app = await browserApp(undefined, () => new Promise(resolve => { finish = resolve; }));
  app.get('auth-register-tab').dispatch('click');
  app.get('register-username').value = 'alice';
  app.get('register-password').value = app.get('register-password-confirm').value = 'fixture private password';
  app.get('register-form').dispatch('submit');
  app.get('register-form').dispatch('submit');
  assert.equal(app.get('register-form').getAttribute('aria-busy'), 'true');
  assert.equal(app.get('register-form').submitButton.textContent, 'Creating account…');
  assert.equal(app.get('register-password').disabled, true);
  assert.equal(app.get('register-password').value, '');
  finish(new Response(JSON.stringify({detail: 'Unauthorized'}), {status: 401})); await app.settle();
  assert.equal(app.calls.filter(([path]) => path === '/v1/auth/register').length, 1);
  assert.match(app.get('register-error').textContent, /Could not create your account/);
  assert.doesNotMatch(app.get('register-error').textContent, /expired|revoked/);
  assert.equal(app.get('register-form').getAttribute('aria-busy'), null);
  assert.equal(app.get('register-password').disabled, false);
  assert.equal(app.get('register-form').submitButton.children[0].textContent, 'Create account');
  assert.equal(app.client.token, '');
});

test('password sign-in shows a busy label and restores its original button content on failure', async () => {
  let finish;
  const app = await browserApp(undefined, undefined, {
    '/v1/auth/login': () => new Promise(resolve => { finish = resolve; }),
  });
  const button = app.get('password-form').submitButton, original = button.children[0];
  app.get('login-username').value = 'alice'; app.get('login-password').value = 'private password';
  app.get('password-form').dispatch('submit');
  assert.equal(button.textContent, 'Signing in…');
  assert.equal(app.get('login-password').value, '');
  finish(new Response(JSON.stringify({detail: 'Check your credentials'}), {status: 401})); await app.settle();
  assert.equal(button.children[0], original);
  assert.equal(app.get('password-form').getAttribute('aria-busy'), null);
  assert.equal(app.get('login-password').disabled, false);
  assert.equal(app.get('notice').textContent, 'Check your credentials');
});

test('registration activates session, clears credentials and shows a useful empty job state', async () => {
  const app = await browserApp();
  app.get('auth-register-tab').dispatch('click');
  app.get('register-username').value = ' alice ';
  app.get('register-password').value = app.get('register-password-confirm').value = 'fixture private password';
  app.get('register-form').dispatch('submit'); await app.settle();
  const call = app.calls.find(([path]) => path === '/v1/auth/register');
  assert.deepEqual(JSON.parse(call[1].body), {username: 'alice', password: 'fixture private password', session_mode: 'bearer'});
  assert.equal(app.client.token, 'new-session');
  assert.equal(app.get('workspace').hidden, false);
  assert.equal(app.get('signin-panel').hidden, true);
  assert.equal(app.document.activeElement.id, 'meal-style');
  assert.match(app.get('job-list').following.textContent, /No requests yet/);
  assert.equal(app.get('job-list').children.length, 0);
  assert.equal(app.get('register-password').value, '');
  assert.equal(app.get('register-password-confirm').value, '');
  app.get('register-password').value = 'leftover'; app.pagehide();
  assert.equal(app.get('register-password').value, ''); assert.equal(app.client.token, '');
});

test('cookie registration suppresses expected resume 401 and activates CSRF session without bearer headers', async () => {
  const app = await browserApp({registration: true, password_login: true, cookie_sessions: true},
    () => new Response(JSON.stringify({session_mode: 'cookie', csrf_token: 'new-csrf'})),
    {'/v1/auth/session': () => new Response('{}', {status: 401})});
  assert.equal(app.get('notice').textContent, '');
  app.get('auth-register-tab').dispatch('click');
  app.get('register-username').value = 'alice';
  app.get('register-password').value = app.get('register-password-confirm').value = 'fixture private password';
  app.get('register-form').dispatch('submit'); await app.settle();
  const registration = app.calls.find(([path]) => path === '/v1/auth/register');
  assert.equal(JSON.parse(registration[1].body).session_mode, 'cookie');
  assert.equal(app.client.cookieMode, true); assert.equal(app.client.csrf, 'new-csrf');
  assert.equal(app.client.token, '');
  assert.equal(app.get('workspace').hidden, false);
  assert.equal(app.document.activeElement.id, 'meal-style');
  assert.equal(app.calls.find(([path]) => path === '/v1/me')[1].headers.get('Authorization'), null);
});

test('automatic cookie resume never moves focus while the user is already entering a username', async () => {
  let finishCapabilities;
  const app = await browserApp(undefined, undefined, {
    '/v1/auth/capabilities': () => new Promise(resolve => { finishCapabilities = resolve; }),
    '/v1/auth/session': {session_mode: 'cookie', csrf_token: 'resumed-csrf'},
  });
  app.get('login-username').focus(); app.get('login-username').value = 'typing';
  finishCapabilities(new Response(JSON.stringify({registration: true, password_login: true, cookie_sessions: true})));
  await app.settle();
  assert.equal(app.client.cookieMode, true);
  assert.equal(app.get('workspace').hidden, false);
  assert.equal(app.document.activeElement.id, 'login-username');
  assert.equal(app.get('login-username').value, 'typing');
});

test('ending the session during registration cannot activate a late response', async () => {
  let finish;
  const app = await browserApp(undefined, () => new Promise(resolve => { finish = resolve; }));
  app.get('auth-register-tab').dispatch('click');
  app.get('register-password').value = app.get('register-password-confirm').value = 'fixture private password';
  app.get('register-form').dispatch('submit'); app.pagehide();
  finish(new Response(JSON.stringify({token: 'late-token', session_mode: 'bearer'}))); await app.settle();
  assert.equal(app.client.token, ''); assert.equal(app.get('workspace').hidden, true);
  assert.equal(app.get('register-password').value, '');
  assert.equal(app.get('register-form').getAttribute('aria-busy'), null);
});

test('token and invitation sign-in remain available when password auth and registration are disabled', async () => {
  const app = await browserApp({password_login: false}, undefined,
    {'/v1/invitations/accept': {token: 'invitation-session', session_mode: 'bearer'}});
  app.get('session-token').value = 'token-session';
  app.get('signin-form').dispatch('submit'); await app.settle();
  assert.equal(app.client.token, 'token-session'); assert.equal(app.get('workspace').hidden, false);
  app.pagehide();
  app.get('invite-token').value = 'invitation';
  app.get('invite-form').dispatch('submit'); await app.settle();
  assert.equal(app.client.token, 'invitation-session');
  assert.equal(app.get('invite-token').value, ''); assert.equal(app.get('invite-password').value, '');
  assert.equal(app.get('auth-tabs').hidden, true);
});

test('pending jobs explain the wait and keep result downloads hidden', async () => {
  const job = {job_id: 'queued-job', kind: 'recipe', status: 'queued', phase: 'waiting', created_at: '2026-10-01'};
  const app = await browserApp({password_login: false}, undefined, {
    '/v1/jobs?offset=0&limit=10': {items: [job], total: 1, offset: 0, limit: 10},
    '/v1/jobs/queued-job': job,
  });
  app.get('session-token').value = 'token'; app.get('signin-form').dispatch('submit'); await app.settle();
  app.get('job-list').children[0].children[1].dispatch('click'); await app.settle();
  assert.match(app.get('result').children[0].textContent, /request is queued/);
  assert.equal(app.get('downloads').hidden, true);
  app.pagehide();
});

for (const overlapping of [false, true]) test(`recipe busy state cannot leak across sessions (new request pending: ${overlapping})`, async () => {
  const finishes = [];
  const app = await browserApp({password_login: false}, undefined, {
    '/v1/recipes': () => new Promise(resolve => finishes.push(resolve)),
  });
  app.get('session-token').value = 'alice-session';
  app.get('signin-form').dispatch('submit'); await app.settle();
  app.get('recipe-form').dispatch('submit');
  assert.equal(finishes.length, 1);
  assert.equal(app.get('generate').disabled, true);
  app.get('signout').dispatch('click'); await app.settle();
  assert.equal(app.get('recipe-form').getAttribute('aria-busy'), null);
  app.get('session-token').value = 'bob-session';
  app.get('signin-form').dispatch('submit'); await app.settle();
  assert.equal(app.get('generate').disabled, false);
  if (overlapping) {
    app.get('recipe-form').dispatch('submit');
    assert.equal(finishes.length, 2);
    assert.equal(app.get('recipe-form').getAttribute('aria-busy'), 'true');
  }
  finishes[0](new Response(JSON.stringify({job_id: 'alice-job', kind: 'recipe', status: 'queued'}), {status: 202}));
  await app.settle();
  assert.equal(app.get('generate').disabled, overlapping);
  assert.equal(app.get('recipe-form').getAttribute('aria-busy'), overlapping ? 'true' : null);
  assert.equal(app.calls.some(([path]) => path === '/v1/jobs/alice-job'), false);
  assert.equal(app.client.token, 'bob-session');
  if (overlapping) {
    app.get('recipe-form').dispatch('submit');
    assert.equal(finishes.length, 2, 'old cleanup must not permit a duplicate new submission');
    finishes[1](new Response(JSON.stringify({detail: 'Try again'}), {status: 503})); await app.settle();
    assert.equal(app.get('generate').disabled, false);
    assert.equal(app.get('recipe-form').getAttribute('aria-busy'), null);
    const calls = app.calls.filter(([path]) => path === '/v1/recipes');
    assert.notEqual(calls[0][1].headers.get('Idempotency-Key'), calls[1][1].headers.get('Idempotency-Key'));
    assert.equal(calls[1][1].headers.get('Authorization'), 'Bearer bob-session');
  }
});

test('cookie login, resume and mutations keep secrets out of bearer headers', async () => {
  const calls = [];
  const client = new GroceryClient(async (path, options) => {
    calls.push([path, options]);
    return new Response(JSON.stringify({session_mode: 'cookie', csrf_token: 'csrf-only'}));
  });
  await client.login('alice', 'long fixture password', 'cookie');
  await client.refresh('kupi', 'refresh-key', 'a'.repeat(24));
  assert.equal(client.token, '');
  assert.equal(calls[1][1].headers.get('Authorization'), null);
  assert.equal(calls[1][1].headers.get('X-CSRF-Token'), 'csrf-only');
  assert.equal(calls[1][1].credentials, 'same-origin');
  assert.equal(JSON.parse(calls[1][1].body).profile_fingerprint, 'a'.repeat(24));
  await client.resume(); await client.logout();
  assert.equal(calls[2][1].method, 'GET');
  assert.equal(calls[3][1].headers.get('X-CSRF-Token'), 'csrf-only');
  client.clearSession(); assert.equal(client.csrf, ''); assert.equal(client.cookieMode, false);
});

test('nutrition, loyalty, time and pantry priority preserve typed request parity', () => {
  const value = recipePayload({...form, min_protein_g: '70.0001', max_kcal: '800', max_minutes: '30', retailer_ids: ['billa'], allow_loyalty: false, use_first: ['rice'], seasonings_available: true}, capabilities, [{ingredient: 'rice', quantity: '500g'}]);
  assert.equal(value.min_protein_g, '70.0001');
  assert.equal(value.max_kcal, '800');
  assert.equal(value.max_minutes, 30);
  assert.equal(value.allow_loyalty, false);
  assert.deepEqual(value.use_first, ['rice']);
  assert.throws(() => recipePayload({...form, max_minutes: '2.5'}, capabilities, []));
  assert.throws(() => recipePayload({...form, use_first: ['rice']}, capabilities, []));
});

test('combined request preserves per-source fingerprints and advertised permissions', () => {
  const cap = {...capabilities, sources: ['kupi', 'tesco'], combined_sources: true};
  const selection = {source_ids: ['kupi', 'tesco'], profile_fingerprints: {kupi: 'a'.repeat(24), tesco: 'b'.repeat(24)}};
  const payload = recipePayload({...form, ...selection}, cap, []);
  assert.deepEqual(payload.source_ids, selection.source_ids);
  assert.deepEqual(payload.profile_fingerprints, selection.profile_fingerprints);
  assert.throws(() => recipePayload({...form, ...selection}, {...cap, combined_sources: false}, []));
  assert.throws(() => recipePayload({...form, ...selection, profile_fingerprint: 'legacy'}, cap, []));
  assert.throws(() => recipePayload({...form, profile_fingerprints: {rohlik: 'a'.repeat(24)}}, cap, []));
});

test('profiles are explicit, fingerprint-validated and discoverable without refreshing', async () => {
  assert.equal(recipePayload(form, capabilities, []).profile_fingerprint, undefined);
  const fingerprint = 'abcdef0123456789abcdef01';
  assert.equal(recipePayload({...form, profile_fingerprint: fingerprint}, capabilities, []).profile_fingerprint, fingerprint);
  assert.throws(() => recipePayload({...form, profile_fingerprint: 'newest'}, capabilities, []));
  const paths = [];
  const client = new GroceryClient(async path => { paths.push(path); return new Response('[]'); });
  await client.collections('kupi');
  await client.offers({source: 'kupi', profile_fingerprint: fingerprint});
  assert.equal(paths[0], '/v1/collections?source=kupi');
  assert.match(paths[1], /profile_fingerprint=abcdef0123456789abcdef01/);
});

test('pantry and budget cross JSON as exact decimal strings', () => {
  const payload = recipePayload(form, capabilities, [{ingredient: 'rice', quantity: '1.000001kg'}, {ingredient: 'oil', quantity: 'available'}]);
  assert.equal(payload.max_cost_per_serving_czk, '12.3400');
  assert.equal(payload.pantry.rice, '1.000001kg');
  assert.equal(payload.pantry.oil, 'available');
  assert.equal(payload.servings, 2);
});
test('unknown, duplicate and zero stock is rejected before submission', () => {
  for (const rows of [[{ingredient: 'rice', quantity: '0g'}], [{ingredient: 'rice', quantity: '5lb'}], [{ingredient: 'unknown', quantity: '5g'}], [{ingredient: 'rice', quantity: '1g'}, {ingredient: 'rice', quantity: '2g'}]]) assert.throws(() => recipePayload(form, capabilities, rows));
  assert.throws(() => recipePayload({...form, budget: '0.00'}, capabilities, []));
});
test('request uses a header token and stable submission key, refuses redirects', async () => {
  const calls = [];
  const client = new GroceryClient(async (path, options) => { calls.push([path, options]); return new Response(JSON.stringify({job_id: 'job', status: 'queued'}), {status: 202}); });
  client.token = 'private-session-token';
  const payload = recipePayload(form, capabilities, []);
  await client.submit(payload, 'retry-key'); await client.submit(payload, 'retry-key');
  assert.equal(calls[0][0], '/v1/recipes');
  assert.equal(calls[0][1].headers.get('Authorization'), 'Bearer private-session-token');
  assert.equal(calls[1][1].headers.get('Idempotency-Key'), 'retry-key');
  assert.equal(calls[0][1].redirect, 'error');
  assert.equal(calls[0][1].credentials, 'omit');
});
test('private report download includes authorization; status errors are explicit', async () => {
  const calls = [];
  const client = new GroceryClient(async (path, options) => { calls.push(options); return new Response('<p>Report</p>'); });
  client.token = 'session';
  assert.equal(await client.report('id'), '<p>Report</p>');
  assert.equal(calls[0].headers.get('Authorization'), 'Bearer session');
  client.fetcher = async () => new Response(JSON.stringify({detail: 'Session expired'}), {status: 401});
  await assert.rejects(client.job('id'), error => error instanceof ApiError && error.status === 401);
});
test('job identifiers are escaped and pagination is bounded by the caller', async () => {
  const paths = [];
  const client = new GroceryClient(async path => { paths.push(path); return new Response('{}'); });
  await client.job('../secret'); await client.jobs(10, 10);
  assert.equal(paths[0], '/v1/jobs/..%2Fsecret');
  assert.equal(paths[1], '/v1/jobs?offset=10&limit=10');
});

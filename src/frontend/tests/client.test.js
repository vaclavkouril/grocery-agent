// Offline contract tests for the independent website application.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ApiError, GroceryClient, recipePayload} from '../assets/client.js';

const capabilities = {providers: ['template'], cache_policies: ['cache-only'], ingredients: ['rice', 'oil'], sources: ['kupi']};
const form = {provider: 'template', model: '', cache_policy: 'cache-only', meal_style: 'main', servings: '2', max_stores: '1', budget: '12.3400', exclusions: []};

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

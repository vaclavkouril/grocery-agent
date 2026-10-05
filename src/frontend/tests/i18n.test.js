import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Language, dictionaries, detectLanguage, errorKeys, errorMessage, language, displayDecimal, languageStorageKey} from '../assets/i18n.js';
import {ApiError, GroceryClient, recipePayload, pantryItems, presetParameters} from '../assets/client.js';
import {AccountUI, fieldLabels} from '../assets/account.js';

test('every authored text node, placeholder and accessible name has both translations', async () => {
  const html = (await readFile(new URL('../index.html', import.meta.url), 'utf8')).replace(/<svg[\s\S]*?<\/svg>/g, '').replace(/<script[\s\S]*?<\/script>/g, '');
  const literal = new Set(['grocery', 'agent', 'groceryagent', '●', '↓', '↗', '+', '→', '◌', '01', '02', '03', '04', 'Čeština', 'English', 'SimpleX', 'billa, tesco']);
  const keys = [...html.matchAll(/>([^<>]+)</g)].map(match => match[1].trim()).filter(Boolean);
  keys.push(...[...html.matchAll(/(?:placeholder|aria-label)="([^"]+)"/g)].map(match => match[1]));
  for (const key of keys) if (!literal.has(key)) {
    assert.ok(Object.hasOwn(dictionaries.en, key), `Missing English: ${key}`);
    assert.ok(Object.hasOwn(dictionaries.cs, key), `Missing Czech: ${key}`);
  }
  assert.deepEqual(Object.keys(dictionaries.en).sort(), Object.keys(dictionaries.cs).sort());
  for (const [key, translated] of Object.entries(dictionaries.cs)) {
    assert.ok(translated.trim());
    assert.deepEqual([...translated.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort(), [...key.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort(), key);
  }
});

test('all dynamic t calls and field labels have complete translations', async () => {
  for (const file of ['app.js', 'account.js', 'choices.js']) {
    const source = await readFile(new URL(`../assets/${file}`, import.meta.url), 'utf8');
    for (const match of source.matchAll(/\bt\('([^']+)'/g)) assert.ok(Object.hasOwn(dictionaries.cs, match[1]), `${file}: ${match[1]}`);
  }
  for (const label of Object.values(fieldLabels)) assert.ok(Object.hasOwn(dictionaries.cs, label), label);
});

test('typed recipe schema fields are represented, including generated request ID', async () => {
  const source = await readFile(new URL('../../grocery_agent/recipes.py', import.meta.url), 'utf8');
  const contract = source.split('class RecipeRequest(DomainModel):')[1].split('    @field_validator')[0];
  const fields = [...contract.matchAll(/^    (\w+):/gm)].map(match => match[1]);
  assert.deepEqual(fields.sort(), Object.keys(fieldLabels).sort());
});

test('language detection uses supported browser preferences and Czech fallback', () => {
  assert.equal(detectLanguage(['fr-FR', 'en-US', 'cs-CZ']), 'en');
  assert.equal(detectLanguage(['cs-CZ', 'en-US']), 'cs');
  assert.equal(detectLanguage(['de-DE']), 'cs'); assert.equal(detectLanguage([]), 'cs');
});

test('only explicit UI language is persisted; account defaults never write browser storage', () => {
  const calls = [], storage = {getItem() { return null; }, setItem(...args) { calls.push(args); }};
  const state = new Language(storage, ['en-US']);
  state.account('cs'); assert.equal(state.current, 'cs'); assert.equal(calls.length, 0);
  state.choose('en'); assert.deepEqual(calls, [[languageStorageKey, 'en']]);
  state.account('cs'); state.clearAccount(); assert.equal(state.current, 'en');
  state.choose('invalid'); assert.equal(calls.length, 1);
  const denied = new Language({getItem() { throw Error('Denied'); }, setItem() { throw Error('Denied'); }}, ['cs']);
  denied.choose('en'); assert.equal(denied.current, 'en');
});

for (const code of Object.keys(errorKeys)) test(`structured ${code} error is retained and localized`, async () => {
  const fields = [{type: 'value_error', loc: ['body', 'servings'], msg: 'Invalid servings'}];
  const client = new GroceryClient(async () => new Response(JSON.stringify({detail: 'Technical diagnostic', code, fields}), {status: 422}));
  await assert.rejects(client.settings(), error => {
    assert.equal(error.code, code); assert.deepEqual(error.fields, fields);
    language.current = 'cs'; assert.equal(errorMessage(error), dictionaries.cs[errorKeys[code]]);
    language.current = 'en'; assert.equal(errorMessage(error), errorKeys[code]); return true;
  });
});

test('exact decimals are localized without numerical coercion', () => {
  const amount = '12345678901234567890.123456789000';
  assert.equal(displayDecimal(amount, 'cs'), '12345678901234567890,123456789000');
  assert.equal(displayDecimal(amount, 'en'), amount);
  assert.deepEqual(pantryItems([{ingredient: 'rice', quantity: '1,000001kg'}, {ingredient: 'oil', quantity: 'available'}], ['rice']), {rice: {grams: '1000.001', use_first: true}, oil: {grams: null, use_first: false}});
  assert.deepEqual(pantryItems([{ingredient: 'rice', quantity: '0.000000001kg'}]), {rice: {grams: '0.000001', use_first: false}});
});

test('pantry malformed/zero quantities and duplicates fail before writing', () => {
  for (const rows of [[{ingredient: 'rice', quantity: '0g'}], [{ingredient: 'rice', quantity: '2lb'}], [{ingredient: 'rice', quantity: '-1g'}], [{ingredient: 'rice', quantity: '1g'}, {ingredient: 'rice', quantity: '2g'}]]) assert.throws(() => pantryItems(rows));
});

test('tri-state loyalty and seasonings preserve default, true and false independently of recipe language', () => {
  const caps = {providers: ['template'], cache_policies: ['cache-only'], sources: ['kupi'], ingredients: ['rice']};
  const form = {provider: 'template', cache_policy: 'cache-only', servings: '2', max_stores: '1', meal_style: 'main', exclusions: [], language: 'cs'};
  for (const preference of [undefined, true, false]) {
    const body = recipePayload({...form, seasonings_available: preference, allow_loyalty: preference}, caps, []);
    assert.equal(body.language, 'cs'); assert.equal(Object.hasOwn(body, 'allow_loyalty'), preference !== undefined); assert.equal(body.seasonings_available, preference);
  }
  assert.throws(() => recipePayload({...form, language: 'de'}, caps, []));
});

test('offers serialize repeated retailers, full filters and decimal strings', async () => {
  let path;
  const api = new GroceryClient(async url => { path = url; return new Response('{}'); });
  await api.offers({retailer: ['billa', 'tesco'], category: 'produce', scope: 'national', max_unit_price: '42.0001', include_from: true, include_unavailable: false, unit: 'kg'});
  const query = new URL(path, 'https://fixture.test').searchParams;
  assert.deepEqual(query.getAll('retailer'), ['billa', 'tesco']); assert.equal(query.get('max_unit_price'), '42.0001');
  assert.equal(query.get('include_from'), 'true'); assert.equal(query.get('include_unavailable'), 'false');
});

test('account writes include revisions, CSRF and scoped IDs; presets omit transient stock and request identity', async () => {
  const calls = [], api = new GroceryClient(async (path, options) => { calls.push([path, options]); return new Response('{}'); });
  api.setSession({session_mode: 'cookie', csrf_token: 'private-csrf'});
  await api.saveSettings({ui_language: 'cs', expected_revision: 3}); await api.savePantry({rice: {grams: '1', use_first: false}}, 4);
  const request = {provider: 'template', pantry: {rice: '1g'}, use_first: ['rice'], request_id: 'old', language: 'en'};
  await api.createPreset('Sign out', request); await api.updatePreset('a/b', 'Edited', request, 5); await api.deletePreset('a/b', 6); await api.jobRequest('a/b');
  assert.deepEqual(JSON.parse(calls[1][1].body), {items: {rice: {grams: '1', use_first: false}}, expected_revision: 4});
  for (const [, options] of calls.slice(0, 5)) assert.equal(options.headers.get('X-CSRF-Token'), 'private-csrf');
  for (const index of [2, 3]) assert.deepEqual(JSON.parse(calls[index][1].body).parameters, presetParameters(request));
  assert.equal(calls[3][0], '/v1/me/presets/a%2Fb'); assert.equal(calls[3][1].method, 'PATCH');
  assert.equal(calls[4][0], '/v1/me/presets/a%2Fb?expected_revision=6'); assert.equal(calls[5][0], '/v1/jobs/a%2Fb/request');
});

test('stale or unavailable preset choices cannot partially change the form', () => {
  let applied = false;
  const context = {selectedPreset: () => ({stale: true}), applyParameters() { applied = true; }};
  assert.throws(() => AccountUI.prototype.applyPreset.call(context), /no longer supported/); assert.equal(applied, false);
  const unsupported = {capabilities: () => ({providers: ['template'], cache_policies: ['cache-only'], meal_styles: ['main'], sources: ['kupi'], ingredients: ['rice']}), $() { throw Error('Form must not be accessed'); }};
  assert.throws(() => AccountUI.prototype.applyParameters.call(unsupported, {provider: 'removed-provider'}), /no longer supported/);
  unsupported.capabilities = () => ({providers: ['template'], cache_policies: ['cache-only'], meal_styles: ['main'], sources: ['kupi'], ingredients: ['rice'], models: {template: []}});
  assert.throws(() => AccountUI.prototype.applyParameters.call(unsupported, {provider: 'template', model: 'removed-model'}), /no longer supported/);
  assert.throws(() => AccountUI.prototype.applyParameters.call(unsupported, {profile_fingerprint: 'legacy'}), /no longer supported/);
});

test('late account responses cannot revive data after the session ends', async () => {
  let finish, generation = 1, touched = false;
  const context = {api: {settings: () => new Promise(resolve => { finish = resolve; })}, generation: () => generation,
    operation: async (_, action) => action(generation), $() { touched = true; throw Error('Late DOM access'); }};
  const loading = AccountUI.prototype.loadPreferences.call(context); generation = 2;
  finish({revision: 0, ui_language: 'cs', recipe_language: 'en'}); await loading;
  assert.equal(touched, false); assert.equal(context.settings, undefined);
});

test('revision conflicts preserve local edits and prevent writes until explicit reload', async () => {
  const elements = new Map(); const $ = id => { if (!elements.has(id)) elements.set(id, {value: 'local edit', disabled: false}); return elements.get(id); };
  const context = {busy: new Set(), generation: () => 1, settings: {revision: 2}, stock: {revision: 3}, $, status(_, message) { this.message = message; }};
  await AccountUI.prototype.operation.call(context, 'pantry', async () => { throw new ApiError(409, 'Revision conflict', 'conflict'); });
  assert.equal(context.stock, undefined); assert.equal($('save-pantry').disabled, true); assert.equal($('pantry-rows').value, 'local edit');
  await AccountUI.prototype.operation.call(context, 'preset', async () => { throw new ApiError(409, 'Revision conflict', 'conflict'); });
  assert.equal(context.presetConflict, true); assert.equal($('update-preset').disabled, true); assert.equal($('preset-name').value, 'local edit');
});

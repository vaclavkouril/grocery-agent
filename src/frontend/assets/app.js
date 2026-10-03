// Website application source; independently served or packaged with the backend.
import {ApiError, GroceryClient, recipePayload} from './client.js';

const api = new GroceryClient();
const $ = id => document.getElementById(id);
let capabilities, currentJob, result, timer, generation = 0, authCapabilities = {};
let offerOffset = 0, jobsOffset = 0, offerParameters = {}, pendingSubmission;
let collectionsBySource = {};
let authenticationBusy = false, recipeBusy;
let jobsEmpty;
const pageSize = 10;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function clearPasswords() {
  for (const id of ['login-password', 'invite-password', 'register-password', 'register-password-confirm']) if ($(id)) $(id).value = '';
}
function registrationError(message) {
  const target = $('register-error');
  if (target) { target.textContent = message; target.hidden = !message; }
}
function authenticationMode(register = false, focus = false) {
  register = register && authCapabilities.registration === true;
  if ($('auth-tabs')) $('auth-tabs').hidden = authCapabilities.registration !== true;
  for (const [id, selected] of [['auth-login-tab', !register], ['auth-register-tab', register]]) {
    const tab = $(id);
    if (tab) { tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1; }
  }
  $('password-form').hidden = register || !authCapabilities.password_login;
  $('signin-form').hidden = register;
  const tokenAccess = $('signin-form').closest('details');
  if (tokenAccess) { tokenAccess.hidden = register; tokenAccess.open = !authCapabilities.password_login; }
  $('invite-form').hidden = register;
  const invitation = $('invite-form').closest('details');
  if (invitation) invitation.hidden = register;
  if ($('register-form')) $('register-form').hidden = !register;
  if ($('signin-heading')) $('signin-heading').textContent = register ? 'Create your account' : 'Welcome back';
  if ($('signin-description')) $('signin-description').textContent = register ? 'Choose a username and password to plan meals and keep your reports together.' : 'Plan from your pantry and pick up your meal reports.';
  clearPasswords(); registrationError('');
  if ($('register-password-confirm')) $('register-password-confirm').removeAttribute('aria-invalid');
  if (focus) $(register ? 'register-username' : authCapabilities.password_login ? 'login-username' : 'session-token')?.focus();
}
async function authenticate(form, request, registration = false, resume = false) {
  if (authenticationBusy) return;
  authenticationBusy = true;
  const active = generation;
  let expected = active;
  const controls = [...document.querySelectorAll('#signin-panel input, #signin-panel button')].map(element => [element, element.disabled]);
  const submit = form.querySelector('button[type="submit"]');
  const submitContent = submit ? [...submit.childNodes] : [];
  if (submit) submit.textContent = registration ? 'Creating account…' : 'Signing in…';
  for (const [element] of controls) element.disabled = true;
  form.setAttribute('aria-busy', 'true'); notice(''); registrationError(''); clearPasswords();
  try {
    await request();
    if (active !== generation) { api.clearSession(); return; }
    expected = generation + 1;
    await activateSession();
    if (!resume && expected === generation && capabilities && !$('workspace').hidden) $('meal-style').focus();
  } catch (error) {
    if (expected !== generation) return;
    api.clearSession();
    if (resume && error instanceof ApiError && error.status === 401) return;
    if (capabilities) { endSession(); notice('Could not load your account. Please sign in again.'); return; }
    const message = registration && error instanceof ApiError && error.status === 401
      ? 'Could not create your account. Check your details and try again.'
      : error.message ?? 'Could not sign in. Please try again.';
    if (registration) { registrationError(message); $('register-username')?.focus(); }
    else notice(message);
  } finally {
    clearPasswords(); authenticationBusy = false; form.removeAttribute('aria-busy');
    if (submit) submit.replaceChildren(...submitContent);
    for (const [element, disabled] of controls) element.disabled = disabled;
  }
}
async function run(action) {
  const start = generation;
  try { await action(); } catch (error) {
    if (start !== generation) return;
    if (error instanceof ApiError && error.status === 401) {
      endSession(); notice('Your session expired or was revoked. Sign in again to continue.');
    } else notice(error.message ?? 'Something went wrong. Please try again.');
  }
}
function endSession() {
  generation += 1; clearTimeout(timer); api.clearSession(); capabilities = undefined;
  currentJob = undefined; result = undefined; pendingSubmission = undefined;
  recipeBusy = undefined; $('recipe-form').removeAttribute('aria-busy'); $('generate').disabled = true;
  collectionsBySource = {}; $('extra-source-profiles').replaceChildren();
  $('workspace').hidden = true; $('account').hidden = true; $('signin-panel').hidden = false;
  $('session-token').value = ''; $('invite-token').value = '';
  clearPasswords(); authenticationMode();
  $('pantry-rows').replaceChildren(); $('result').replaceChildren(); $('job-list').replaceChildren();
  for (const id of ['recipe-profile', 'offer-profile']) options($(id), [['', 'Legacy catalogue · coverage unknown']]);
  $('offer-rows').replaceChildren(); $('new-invite-token').textContent = '';
  $('recipe-form').reset(); $('offers-form').reset(); $('new-invite-form').reset();
  $('username').textContent = ''; $('coverage').textContent = '';
  $('current-job').hidden = true; $('downloads').hidden = true;
}
function options(select, entries, selected) {
  select.replaceChildren(...entries.map(([value, label]) => {
    const option = node('option', label); option.value = value; return option;
  }));
  if (selected !== undefined) select.value = selected;
}
function ingredientOptions() { return capabilities.ingredients.map(id => [id, capabilities.ingredient_labels[id] ?? id]); }
function profileEntries(source) {
  return [['', 'Default profile for selected cache policy'], ...(collectionsBySource[source] ?? []).filter(item => item.profile_fingerprint !== 'legacy').map(item => [item.profile_fingerprint, `${item.profile?.name ?? item.profile_fingerprint} · ${(item.coverage.actual_scopes ?? []).join(', ') || 'scope unknown'}`])];
}
function selectedSources() { return [...$('recipe-sources').selectedOptions].map(option => option.value); }
function sourceProfiles() {
  const sources = selectedSources();
  options($('recipe-profile'), profileEntries(sources[0]));
  $('extra-source-profiles').replaceChildren();
  for (const source of sources.slice(1)) {
    const label = node('label', `Saved grocery profile · ${source}`);
    const select = node('select'); select.dataset.source = source; options(select, profileEntries(source));
    label.append(select); $('extra-source-profiles').append(label);
  }
}
function setModel() {
  const provider = $('provider').value;
  const models = capabilities.models[provider] ?? [];
  options($('model'), provider === 'codex' ? [['', 'Configured CLI default'], ...models.map(id => [id, id])] : models.map(id => [id, id]));
  $('model').disabled = provider === 'template';
  const unavailable = provider === 'ollama' && models.length === 0;
  $('generate').disabled = recipeBusy?.generation === generation || !provider || unavailable;
  if (unavailable) notice('No local model is configured for this provider. Choose another provider.');
}
function addPantry() {
  const row = node('div', undefined, 'pantry-row');
  const select = node('select'); options(select, [['', 'Choose ingredient'], ...ingredientOptions()]);
  const quantity = node('input'); quantity.placeholder = '500g, 2kg, available'; quantity.maxLength = 128;
  const ingredientLabel = node('label', 'Ingredient'); ingredientLabel.append(select);
  const quantityLabel = node('label', 'Owned quantity'); quantityLabel.append(quantity);
  const remove = node('button', '×', 'quiet'); remove.type = 'button'; remove.setAttribute('aria-label', 'Remove pantry ingredient');
  remove.addEventListener('click', () => row.remove()); row.append(ingredientLabel, quantityLabel, remove);
  $('pantry-rows').append(row);
}
async function activateSession(token) {
  if (token !== undefined) api.setSession({token, session_mode: 'bearer'});
  const active = ++generation;
  let user, supported;
  try { [user, supported] = await Promise.all([api.me(), api.capabilities()]); }
  catch (error) { if (active === generation) { endSession(); notice(error.message); } return; }
  if (active !== generation) return;
  capabilities = supported; $('username').textContent = user.username;
  $('signin-panel').hidden = true; $('account').hidden = false; $('workspace').hidden = false;
  $('session-token').value = ''; $('invite-token').value = '';
  clearPasswords(); registrationError('');
  $('coverage').textContent = `${supported.sources.join(', ')} · ${supported.location_label ?? supported.scope}`;
  options($('provider'), supported.providers.map(id => [id, id === 'template' ? 'Configured recipes' : id]), supported.default_provider);
  options($('cache-policy'), supported.cache_policies.map(id => [id, id === 'cache-only' ? 'Saved grocery data only' : id]), supported.default_cache_policy);
  options($('meal-style'), supported.meal_styles.map(id => [id, id[0].toUpperCase() + id.slice(1)]));
  options($('exclusions'), ingredientOptions());
  options($('use-first'), ingredientOptions());
  options($('recipe-sources'), supported.sources.map(source => [source, source]), supported.sources[0]);
  $('recipe-sources').multiple = Boolean(supported.combined_sources);
  options($('offer-source'), supported.sources.map(source => [source, source]), supported.sources[0]);
  const properties = supported.recipe_request_schema.properties;
  for (const [id, field] of [['min-protein', 'min_protein_g'], ['max-kcal', 'max_kcal'], ['max-minutes', 'max_minutes'], ['recipe-retailers', 'retailer_ids'], ['recipe-loyalty', 'allow_loyalty'], ['use-first', 'use_first'], ['have-seasonings', 'seasonings_available']]) $(id).disabled = !properties[field];
  for (const [id, key] of [['servings', 'servings'], ['max-stores', 'max_stores']]) {
    $(id).min = properties[key].minimum; $(id).max = properties[key].maximum;
    $(id).value = properties[key].default; $(id).step = '1';
  }
  options($('offer-unit'), [['', 'Any unit'], ...supported.offer_query_schema.$defs.Unit.enum.map(unit => [unit, unit])]);
  $('admin').hidden = user.role !== 'admin'; $('refresh-catalogue').disabled = !supported.refresh_allowed;
  options($('refresh-source'), supported.sources.map(source => [source, source])); refreshProfiles();
  $('pantry-rows').replaceChildren(); addPantry(); setModel();
  currentJob = undefined; result = undefined; offerOffset = jobsOffset = 0;
  const empty = node('div', undefined, 'empty-state');
  const marker = node('span', '◌'); marker.setAttribute('aria-hidden', 'true');
  empty.append(marker, node('h3', 'Your next meal starts here'), node('p', 'Choose your servings and pantry ingredients, then find a recipe. Your plan will include nutrition, a shopping list, and cooking steps.'));
  $('result').replaceChildren(empty);
  $('job-progress').textContent = 'Ready when you are.';
  notice(''); await loadJobs();
  if (active !== generation) return;
  try {
    const collections = await Promise.all(supported.sources.map(source => api.collections(source)));
    if (active !== generation) return;
    collectionsBySource = Object.fromEntries(supported.sources.map((source, index) => [source, collections[index]]));
    options($('offer-profile'), profileEntries($('offer-source').value));
    sourceProfiles();
    $('recipe-profile').disabled = !properties.profile_fingerprint;
  } catch (error) {
    if (active !== generation) return;
    if (error instanceof ApiError && error.status === 401) throw error;
    notice('Saved profiles are unavailable. Legacy catalogue selection remains available.');
  }
}
function refreshProfiles() {
  const profiles = capabilities.refresh_profiles?.[$('refresh-source').value] ?? [];
  options($('refresh-profile'), profiles.length ? profiles.map(id => [id, id]) : [['', 'Configured default']]);
}
function showJob(job) {
  currentJob = job; $('current-job').hidden = false; $('job-reference').textContent = `Request ${job.job_id}`;
  $('job-progress').textContent = `${job.kind === 'refresh' ? 'Grocery refresh' : 'Recipe request'}: ${job.status} · ${job.phase}`;
  $('cancel-job').hidden = !['queued', 'running'].includes(job.status);
  if (['queued', 'running'].includes(job.status)) {
    $('result').replaceChildren(node('p', job.status === 'queued' ? 'Your request is queued. Recipes will appear here when it finishes.' : 'Your request is running. You can keep planning while we check for results.', 'hint'));
  }
}
async function selectJob(job) {
  clearTimeout(timer); result = undefined; $('downloads').hidden = true;
  $('result').replaceChildren(); showJob(job); await checkJob();
}
async function checkJob() {
  if (!currentJob) return;
  clearTimeout(timer); const id = currentJob.job_id, active = generation;
  const job = await api.job(id);
  if (active !== generation || currentJob?.job_id !== id) return;
  showJob(job);
  if (['queued', 'running'].includes(job.status)) {
    timer = setTimeout(() => run(checkJob), 1500); return;
  }
  if (job.status === 'succeeded') {
    const completed = await api.result(id);
    if (active !== generation || currentJob?.job_id !== id) return;
    result = completed; renderResult(completed, job.kind);
    $('downloads').hidden = job.kind !== 'recipe';
  } else $('result').replaceChildren(node('p', job.status === 'cancelled' ? 'This request was cancelled.' : `This request failed: ${job.error_code ?? 'execution failed'}. You can submit a new request.`, 'warning'));
  await loadJobs();
}
function renderResult(value, kind) {
  const target = $('result'); target.replaceChildren();
  if (kind === 'refresh') { target.append(node('p', `Grocery refresh completed. Run ${value.run_id}.`)); return; }
  if (value.status === 'no-feasible-recipe') { target.append(node('p', value.reason ?? 'No recipe meets your preferences.', 'warning')); return; }
  const report = value.report;
  for (const warning of report.warnings ?? []) target.append(node('p', warning, 'warning'));
  target.append(node('p', `Grocery data from ${new Date(report.batch_finished_at).toLocaleString()}.`, 'hint'));
  for (const [index, meal] of report.meals.entries()) {
    const card = node('details', undefined, 'recipe-card'); card.open = index === 0;
    const summary = node('summary');
    summary.append(node('h3', meal.title), node('p', `${meal.minutes} minutes · ${meal.servings} servings · ${meal.stores.length ? meal.stores.join(', ') : 'From your pantry'}`));
    card.append(summary);
    const metrics = node('div', undefined, 'metrics');
    for (const label of [`${meal.usage_cost_per_serving_czk} Kč / serving`, `${meal.nutrients_per_serving.protein_g} g protein`, `${meal.nutrients_per_serving.kcal} kcal`]) metrics.append(node('span', label, 'metric'));
    card.append(metrics, node('h4', 'Ingredients & shopping'));
    const ingredients = node('ul');
    for (const line of meal.lines) ingredients.append(node('li', `${line.price.label}: ${line.required_grams} g total · ${line.owned_grams} g owned · ${line.purchased_grams} g to buy`));
    card.append(ingredients, node('h4', 'Preparation'));
    const steps = node('ol'); for (const step of meal.steps) steps.append(node('li', step)); card.append(steps); target.append(card);
  }
  target.append(node('p', 'Nutrition is an estimate. Costs reflect ingredients used, not complete packages or checkout charges.', 'hint'));
}
function pagination(prefix, total, offset, limit) {
  $(prefix + '-prev').disabled = offset === 0;
  $(prefix + '-next').disabled = offset + limit >= total;
  $(prefix + '-count').textContent = total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : 'No results';
}
async function loadJobs() {
  const active = generation; const page = await api.jobs(jobsOffset, pageSize);
  if (active !== generation) return;
  $('job-list').replaceChildren();
  if (!jobsEmpty) { jobsEmpty = node('p', undefined, 'hint'); $('job-list').after(jobsEmpty); }
  jobsEmpty.hidden = page.items.length > 0;
  jobsEmpty.textContent = page.total ? 'No requests on this page. Go back to see earlier requests.' : 'No requests yet. Choose your meal and pantry ingredients to create your first recipe request.';
  for (const job of page.items) {
    const item = node('li'); item.append(node('span', `${job.kind === 'refresh' ? 'Grocery refresh' : 'Recipe'} · ${job.status} · ${new Date(job.created_at).toLocaleString()}`));
    const open = node('button', 'Open', 'quiet'); open.addEventListener('click', () => run(() => selectJob(job))); item.append(open); $('job-list').append(item);
  }
  pagination('jobs', page.total, page.offset, page.limit);
}
async function loadOffers() {
  const active = generation;
  const source = $('offer-source').value;
  const page = await api.offers({...offerParameters, source, ...(offerParameters.profile_fingerprint ? {} : {scope: capabilities.source_scopes?.[source]?.[0] ?? capabilities.scope}), offset: offerOffset, limit: pageSize});
  if (active !== generation) return;
  const state = page.state;
  $('offer-status').textContent = `${new Date(state.batch_finished_at).toLocaleString()} · fresh until ${new Date(state.fresh_until).toLocaleString()}${state.warnings.length ? ' · ' + state.warnings.join(' ') : ''}`;
  $('offer-rows').replaceChildren();
  for (const {offer, observed_at} of page.items) {
    const row = node('tr'); const product = node('td');
    const link = node('a', offer.product.name);
    try { const url = new URL(offer.source_url); if (['http:', 'https:'].includes(url.protocol)) { link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; } } catch { /* Keep malformed links inert. */ }
    product.append(link, node('span', offer.scope, 'hint'));
    if (offer.promotion?.conditions) product.append(node('span', offer.promotion.conditions, 'hint'));
    if (offer.promotion?.requires_loyalty) product.append(node('span', 'Loyalty price', 'hint'));
    row.append(product, node('td', offer.product.store_id), node('td', `${offer.price_qualifier === 'from' ? 'From ' : ''}${offer.current_price} ${offer.currency} / ${offer.price_basis.amount} ${offer.price_basis.unit}`), node('td', `${offer.unit_price.amount} ${offer.currency} / ${offer.unit_price.unit}`), node('td', new Date(observed_at).toLocaleString()));
    $('offer-rows').append(row);
  }
  pagination('offers', page.total, page.offset, page.limit);
}
function download(body, type, filename) {
  const url = URL.createObjectURL(new Blob([body], {type})); const link = node('a');
  link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

for (const [id, register] of [['auth-login-tab', false], ['auth-register-tab', true]]) {
  $(id)?.addEventListener('click', () => { if (!authenticationBusy) authenticationMode(register, true); });
  $(id)?.addEventListener('keydown', event => {
    if (authenticationBusy || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === 'Home' ? false : event.key === 'End' ? true : !register;
    authenticationMode(target); $(target ? 'auth-register-tab' : 'auth-login-tab')?.focus();
  });
}
$('signin-form').addEventListener('submit', event => {
  event.preventDefault(); const token = $('session-token').value.trim(); $('session-token').value = '';
  authenticate(event.currentTarget, () => api.setSession({token, session_mode: 'bearer'}));
});
$('invite-form').addEventListener('submit', event => {
  event.preventDefault(); const token = $('invite-token').value.trim(), password = $('invite-password').value;
  $('invite-token').value = '';
  authenticate(event.currentTarget, () => api.acceptInvitation(token, password, authCapabilities.cookie_sessions ? 'cookie' : 'bearer'));
});
$('password-form').addEventListener('submit', event => {
  event.preventDefault(); const username = $('login-username').value.trim(), password = $('login-password').value;
  authenticate(event.currentTarget, () => api.login(username, password, authCapabilities.cookie_sessions ? 'cookie' : 'bearer'));
});
$('register-form')?.addEventListener('submit', event => {
  event.preventDefault();
  if (authenticationBusy || authCapabilities.registration !== true) return;
  const username = $('register-username').value.trim(), password = $('register-password').value;
  if (password !== $('register-password-confirm').value) {
    clearPasswords(); registrationError('Passwords do not match. Please enter the same password in both fields.');
    $('register-password-confirm').setAttribute('aria-invalid', 'true'); $('register-password').focus(); return;
  }
  $('register-password-confirm').removeAttribute('aria-invalid');
  authenticate(event.currentTarget, () => api.register(username, password, authCapabilities.cookie_sessions ? 'cookie' : 'bearer'), true);
});
$('signout').addEventListener('click', () => run(async () => { try { await api.logout(); } finally { endSession(); notice('Signed out.'); } }));
$('provider').addEventListener('change', () => { notice(''); setModel(); });
$('recipe-sources').addEventListener('change', sourceProfiles);
$('offer-source').addEventListener('change', () => { options($('offer-profile'), profileEntries($('offer-source').value)); offerParameters = {}; offerOffset = 0; $('offer-rows').replaceChildren(); pagination('offers', 0, 0, pageSize); });
$('add-pantry').addEventListener('click', addPantry);
$('recipe-form').addEventListener('submit', event => { event.preventDefault(); run(async () => {
  if (recipeBusy?.generation === generation) return;
  const body = recipePayload({provider: $('provider').value, model: $('model').disabled ? '' : $('model').value, cache_policy: $('cache-policy').value, meal_style: $('meal-style').value, servings: $('servings').value, max_stores: $('max-stores').value, budget: $('budget').value.trim(), exclusions: [...$('exclusions').selectedOptions].map(option => option.value)}, capabilities, [...$('pantry-rows').children].map(row => ({ingredient: row.querySelector('select').value, quantity: row.querySelector('input').value.trim()})));
  body.source_ids = selectedSources();
  if (!body.source_ids.length) throw new Error('Choose at least one grocery source.');
  if (!$('recipe-profile').disabled) {
    const profiles = {};
    if ($('recipe-profile').value) profiles[body.source_ids[0]] = $('recipe-profile').value;
    for (const select of $('extra-source-profiles').querySelectorAll('select')) if (select.value) profiles[select.dataset.source] = select.value;
    if (body.source_ids.length === 1 && profiles[body.source_ids[0]]) body.profile_fingerprint = profiles[body.source_ids[0]];
    else if (Object.keys(profiles).length) body.profile_fingerprints = profiles;
  }
  const extra = recipePayload({provider: $('provider').value, cache_policy: $('cache-policy').value, meal_style: $('meal-style').value, servings: $('servings').value, max_stores: $('max-stores').value, exclusions: body.exclusions,
    min_protein_g: $('min-protein').disabled ? '' : $('min-protein').value.trim(), max_kcal: $('max-kcal').disabled ? '' : $('max-kcal').value.trim(), max_minutes: $('max-minutes').disabled ? '' : $('max-minutes').value,
    retailer_ids: $('recipe-retailers').disabled || !$('recipe-retailers').value.trim() ? [] : $('recipe-retailers').value.split(',').map(id => id.trim()),
    allow_loyalty: $('recipe-loyalty').disabled || !$('recipe-loyalty').value ? undefined : $('recipe-loyalty').value === 'true', use_first: $('use-first').disabled ? [] : [...$('use-first').selectedOptions].map(option => option.value), seasonings_available: !$('have-seasonings').disabled && $('have-seasonings').checked ? true : undefined}, capabilities, [...$('pantry-rows').children].map(row => ({ingredient: row.querySelector('select').value, quantity: row.querySelector('input').value.trim()})));
  for (const field of ['min_protein_g', 'max_kcal', 'max_minutes', 'retailer_ids', 'allow_loyalty', 'use_first', 'seasonings_available']) if (Object.hasOwn(extra, field)) body[field] = extra[field];
  const serialized = JSON.stringify(body); if (pendingSubmission?.serialized !== serialized) pendingSubmission = {serialized, key: crypto.randomUUID()};
  const active = generation, submission = {generation: active}; recipeBusy = submission; $('generate').disabled = true; $('recipe-form').setAttribute('aria-busy', 'true'); notice('');
  try { const job = await api.submit(body, pendingSubmission.key); if (active !== generation) return;
    pendingSubmission = undefined; await selectJob(job); await loadJobs();
  } finally {
    if (recipeBusy === submission) { recipeBusy = undefined; $('recipe-form').removeAttribute('aria-busy'); if (active === generation) setModel(); }
  }
}); });
$('check-job').addEventListener('click', () => run(checkJob));
$('cancel-job').addEventListener('click', () => run(async () => { const id = currentJob.job_id; await api.cancel(id); if (currentJob?.job_id === id) await checkJob(); }));
$('download-html').addEventListener('click', () => run(async () => { const id = currentJob.job_id, active = generation; const html = await api.report(id); if (active === generation) download(html, 'text/html', `recipe-${id}.html`); }));
$('download-json').addEventListener('click', () => { if (result && currentJob) download(JSON.stringify(result, null, 2), 'application/json', `recipe-${currentJob.job_id}.json`); });
$('offers-form').addEventListener('submit', event => { event.preventDefault(); run(async () => {
  offerParameters = {sort: $('offer-sort').value, allow_loyalty: $('offer-loyalty').checked};
  if ($('offer-profile').value) offerParameters.profile_fingerprint = $('offer-profile').value;
  for (const [key, id] of [['search', 'offer-search'], ['unit', 'offer-unit'], ['retailer', 'offer-retailer']]) if ($(id).value.trim()) offerParameters[key] = $(id).value.trim();
  if (offerParameters.sort === 'unit_price' && !offerParameters.unit) throw new Error('Choose a comparable unit before sorting by unit price.');
  offerOffset = 0; await loadOffers();
}); });
for (const [id, delta, type] of [['offers-prev', -pageSize, 'offers'], ['offers-next', pageSize, 'offers'], ['jobs-prev', -pageSize, 'jobs'], ['jobs-next', pageSize, 'jobs']]) $(id).addEventListener('click', () => run(async () => { if (type === 'offers') { offerOffset = Math.max(0, offerOffset + delta); await loadOffers(); } else { jobsOffset = Math.max(0, jobsOffset + delta); await loadJobs(); } }));
$('reload-jobs').addEventListener('click', () => run(loadJobs));
$('refresh-source').addEventListener('change', refreshProfiles);
$('refresh-catalogue').addEventListener('click', () => run(async () => { const active = generation; const job = await api.refresh($('refresh-source').value, crypto.randomUUID(), $('refresh-profile').value); if (active === generation) await selectJob(job); }));
$('new-invite-form').addEventListener('submit', event => { event.preventDefault(); run(async () => { const active = generation; const response = await api.invite($('invite-username').value); if (active === generation) $('new-invite-token').textContent = response.token; }); });
window.addEventListener('pagehide', endSession);
async function initializeAuthentication() {
  const active = generation;
  try {
    const supported = await api.authCapabilities();
    if (active !== generation || capabilities) return;
    authCapabilities = supported;
    authenticationMode();
    $('invite-password').hidden = $('invite-password-label').hidden = !supported.password_login;
    if (supported.cookie_sessions && !authenticationBusy) await authenticate($('password-form'), () => api.resume(), false, true);
  } catch { /* Older backends keep the token-only interface. */ }
}
initializeAuthentication();

// Website application source; independently served or packaged with the backend.
import {ApiError, GroceryClient, recipePayload, decimal} from './client.js';
import {language, dictionaries, t, errorMessage, staticTranslations, displayDecimal, sourceKey} from './i18n.js';
import {AccountUI, fieldLabels} from './account.js';
import {choiceList, retailerChoices} from './choices.js';
import {clearValidation, showValidation} from './validation.js';

const api = new GroceryClient();
const $ = id => document.getElementById(id);
let capabilities, currentJob, result, timer, generation = 0, authCapabilities = {};
let offerOffset = 0, jobsOffset = 0, offerParameters = {}, pendingSubmission;
let collectionsBySource = {};
let authenticationBusy = false, recipeBusy;
let jobsEmpty;
const pageSize = 10;
const translateStatic = staticTranslations(document.documentElement);
const accountUI = new AccountUI({api, $, node, run: action => run(action), generation: () => generation,
  capabilities: () => capabilities, addPantry, syncPantry, recipe: includePantry => readRecipe(includePantry), relabel: () => relabel(), profiles: sourceProfiles, profileOptions: profileEntries, validation: validationError});
const choices = [];
let summaryRequest, lastOffers, lastJobs;
let adminAllowed = false;
let modelProvider;
const selectedModels = new Map();
const cuisineKeys = {greek: 'Greek', chinese: 'Chinese', vietnamese: 'Vietnamese', indian: 'Indian', mexican: 'Mexican', italian: 'Italian', thai: 'Thai', japanese: 'Japanese', korean: 'Korean', french: 'French', spanish: 'Spanish', mediterranean: 'Mediterranean', czech: 'Czech', middle_eastern: 'Middle Eastern'};
let displayedJob, displayedRequest, bookmarkOffset = 0, lastBookmarks;
let selection = 0, bookmarkLoad = 0;
const bookmarkedMeals = new Map(), bookmarkBusy = new Set();

function node(tag, text, className, literal = false) {
  const element = document.createElement(tag);
  if (text !== undefined) {
    if (tag === 'label') {
      const caption = document.createElement('span'); caption.textContent = literal ? text : t(text);
      if (!literal && Object.hasOwn(dictionaries.en, text)) caption.dataset.i18n = text; element.append(caption);
    } else { element.textContent = literal ? text : t(text); if (!literal && Object.hasOwn(dictionaries.en, text)) element.dataset.i18n = text; }
  }
  if (className) element.className = className;
  return element;
}
function notice(message) { message = sourceKey(message); $('notice').textContent = t(message); $('notice').dataset.i18n = message; $('notice').hidden = !message; }
function clearPasswords() {
  for (const id of ['login-password', 'invite-password', 'register-password', 'register-password-confirm']) if ($(id)) $(id).value = '';
}
function registrationError(message) {
  const target = $('register-error');
  if (target) { message = sourceKey(message); target.textContent = t(message); target.dataset.i18n = message; target.hidden = !message; }
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
  if ($('signin-heading')) $('signin-heading').textContent = t(register ? 'Create your account' : 'Welcome back');
  if ($('signin-description')) $('signin-description').textContent = t(register ? 'Choose a username and password to plan meals and keep your reports together.' : 'Plan from your pantry and pick up your meal reports.');
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
  if (submit) submit.textContent = t(registration ? 'Creating account…' : 'Signing in…');
  for (const [element] of controls) element.disabled = true;
  form.setAttribute('aria-busy', 'true'); notice(''); registrationError(''); clearPasswords();
  try {
    await request();
    if (active !== generation) { api.clearSession(); return; }
    expected = generation + 1;
    await activateSession();
    const focused = document.activeElement;
    if (!resume && expected === generation && capabilities && !$('workspace').hidden &&
        (!focused || focused === document.body || focused.closest?.('#signin-panel'))) $('meal-style').focus();
  } catch (error) {
    if (expected !== generation) return;
    api.clearSession();
    if (resume && error instanceof ApiError && error.status === 401) return;
    if (capabilities) { endSession(); notice('Could not load your account. Please sign in again.'); return; }
    const message = registration && error instanceof ApiError && error.status === 401
      ? 'Could not create your account. Check your details and try again.'
      : errorMessage(error);
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
    } else { notice(errorMessage(error)); validationError(error); }
  }
}
function endSession() {
  clearValidation(document);
  generation += 1; clearTimeout(timer); api.clearSession(); capabilities = undefined;
  currentJob = undefined; result = undefined; pendingSubmission = undefined;
  modelProvider = undefined; selectedModels.clear();
  displayedJob = displayedRequest = lastBookmarks = undefined; bookmarkOffset = 0; bookmarkedMeals.clear(); bookmarkBusy.clear();
  selection += 1; bookmarkLoad += 1;
  $('bookmark-list')?.replaceChildren(); $('bookmarks-status')?.replaceChildren(); $('condiment-options')?.replaceChildren(); $('pantry-quick-add')?.replaceChildren();
  $('request-summary')?.remove();
  for (const choice of choices.splice(0)) choice.destroy();
  $('use-first').replaceChildren();
  summaryRequest = lastOffers = lastJobs = undefined; accountUI.clear(); language.clearAccount(); relabel();
  adminAllowed = false; $('admin').hidden = $('admin-link').hidden = true;
  recipeBusy = undefined; $('recipe-form').removeAttribute('aria-busy'); $('generate').disabled = true;
  collectionsBySource = {}; $('extra-source-profiles').replaceChildren();
  $('workspace').hidden = true; $('account').hidden = true; $('signin-panel').hidden = false;
  $('session-token').value = ''; $('invite-token').value = '';
  clearPasswords(); authenticationMode();
  $('pantry-rows').replaceChildren(); $('result').replaceChildren(); $('job-list').replaceChildren();
  delete $('result').dataset.jobId;
  for (const id of ['recipe-profile', 'offer-profile']) options($(id), [['', 'Legacy catalogue · coverage unknown']]);
  $('offer-rows').replaceChildren(); $('new-invite-token').textContent = '';
  $('recipe-form').reset(); $('offers-form').reset(); $('new-invite-form').reset();
  $('username').textContent = ''; $('coverage').textContent = '';
  $('current-job').hidden = true; $('downloads').hidden = true;
  for (const id of ['job-reference', 'job-progress', 'jobs-count', 'offers-count', 'offer-status']) $(id).textContent = '';
  if (jobsEmpty) { jobsEmpty.textContent = ''; jobsEmpty.hidden = true; }
  for (const details of document.querySelectorAll('.technical-warnings')) details.remove();
}
function options(select, entries, selected) {
  select.replaceChildren(...entries.map(([value, label]) => {
    const option = node('option', label); option.value = value; return option;
  }));
  if (selected !== undefined) select.value = selected;
}
function ingredientOptions() { return capabilities.ingredients.map(id => [id, capabilities.ingredient_localized_labels?.[id]?.[language.current] ?? capabilities.ingredient_labels?.[id] ?? id]); }
function profileEntries(source) {
  const metadata = capabilities?.profile_metadata?.[source];
  const profiles = metadata ? metadata.map(item => [item.fingerprint, `${item.name} · ${item.scope ?? item.coverage?.actual_scopes?.join(', ') ?? t('scope unknown')}`]) : (collectionsBySource[source] ?? []).map(item => [item.profile_fingerprint, `${item.profile?.name ?? item.profile_fingerprint} · ${(item.coverage?.actual_scopes ?? []).join(', ') || t('scope unknown')}`]);
  return [['', t('Default profile for selected cache policy')], ...profiles.filter(([id]) => id !== 'legacy')];
}
function selectedSources() { return [...$('recipe-sources').selectedOptions].map(option => option.value); }
function sourceProfiles() {
  const sources = selectedSources();
  options($('recipe-profile'), profileEntries(sources[0]));
  $('extra-source-profiles').replaceChildren();
  for (const source of sources.slice(1)) {
    const label = node('label', t('Saved grocery profile · {source}', {source}));
    const select = node('select'); select.dataset.source = source; options(select, profileEntries(source));
    label.append(select); $('extra-source-profiles').append(label);
  }
}
function setModel() {
  const provider = $('provider').value;
  const models = capabilities.models[provider] ?? [];
  const current = $('model').value;
  if ((capabilities.models[modelProvider] ?? []).includes(current)) selectedModels.set(modelProvider, current);
  const selected = provider === modelProvider && models.includes(current) ? current
    : models.includes(selectedModels.get(provider)) ? selectedModels.get(provider)
    : models.includes(capabilities.defaults?.model) ? capabilities.defaults.model : models[0] ?? '';
  options($('model'), models.map(id => [id, id]), selected);
  modelProvider = provider;
  const unavailable = provider !== 'template' && models.length === 0;
  $('model').disabled = provider === 'template' || unavailable;
  $('model').required = provider !== 'template';
  $('generate').disabled = recipeBusy?.generation === generation || !provider || unavailable;
  if (unavailable) notice(provider === 'ollama' ? 'No local model is configured for this provider. Choose another provider.' : 'No model is configured for this provider. Choose another provider.');
  updateCuisineControls();
}
function cuisineLabel(id) { return capabilities?.cuisine_labels?.[id]?.[language.current] ?? t(cuisineKeys[id] ?? id); }
function updateCuisineControls() {
  if (!$('cuisine')) return;
  const unavailable = !capabilities?.recipe_request_schema?.properties?.cuisine || $('provider').value === 'template';
  $('cuisine').disabled = unavailable; $('cuisine-hint').hidden = !unavailable;
  for (const button of $('cuisine-presets').querySelectorAll('button')) { button.disabled = unavailable; button.setAttribute('aria-pressed', String(button.dataset.cuisine === $('cuisine').value)); }
}
function renderCuisines() {
  if (!$('cuisine')) return;
  const selected = $('cuisine').value, focused = document.activeElement?.dataset?.cuisine;
  options($('cuisine'), [['', 'Any cuisine'], ...(capabilities.cuisine_ids ?? []).map(id => [id, cuisineLabel(id)])], selected);
  $('cuisine-presets').replaceChildren();
  for (const id of capabilities.cuisine_ids ?? []) {
    const button = node('button', cuisineLabel(id), 'quiet', true); button.type = 'button'; button.dataset.cuisine = id;
    button.addEventListener('click', () => { if (button.disabled) return; $('cuisine').value = id; $('cuisine').dispatchEvent(new Event('change', {bubbles: true})); });
    $('cuisine-presets').append(button);
  }
  updateCuisineControls();
  if (focused) [...$('cuisine-presets').querySelectorAll('button')].find(button => button.dataset.cuisine === focused)?.focus({preventScroll: true});
  if (selected) accountUI.suggestPresetName(cuisineLabel(selected));
}
function syncPantry() {
  const owned = new Set([...$('pantry-rows').children].map(row => row.querySelector('select').value).filter(Boolean));
  for (const option of $('use-first').options ?? []) { option.disabled = !owned.has(option.value); if (option.disabled) option.selected = false; }
  $('use-first').dispatchEvent?.(new Event('change'));
}
function addPantry(ingredient = '') {
  if (typeof ingredient !== 'string') ingredient = '';
  const row = node('div', undefined, 'pantry-row');
  const select = node('select'); options(select, [['', 'Choose ingredient'], ...ingredientOptions()]);
  select.value = ingredient;
  const quantity = node('input'); quantity.id = `pantry-quantity-${crypto.randomUUID()}`; quantity.placeholder = t('500g, 2kg, available'); quantity.maxLength = 128;
  quantity.type = 'hidden';
  const amount = node('input'); amount.type = 'text'; amount.inputMode = 'decimal'; amount.dataset.pantryAmount = ''; amount.id = `${quantity.id}-amount`;
  const unit = node('select'); unit.dataset.pantryUnit = ''; options(unit, [['g', 'Grams'], ['kg', 'Kilograms'], ['available', 'Enough available']]);
  const syncQuantity = () => { amount.disabled = unit.value === 'available'; quantity.value = amount.disabled ? 'available' : amount.value.trim() ? `${decimal(amount.value)}${unit.value}` : ''; };
  amount.addEventListener('input', syncQuantity); unit.addEventListener('change', syncQuantity);
  quantity.addEventListener('input', () => {
    const match = /^(.*?)(kg|g)$/.exec(quantity.value);
    unit.value = quantity.value === 'available' ? 'available' : match?.[2] ?? 'g'; amount.value = match?.[1] ?? ''; amount.disabled = unit.value === 'available';
  });
  const ingredientLabel = node('label', 'Ingredient'); ingredientLabel.append(select);
  const searchLabel = node('label', 'Find an ingredient'); const search = node('input'); search.type = 'search'; searchLabel.append(search);
  search.addEventListener('input', () => { const value = select.value, filter = search.value.toLocaleLowerCase(); options(select, [['', 'Choose ingredient'], ...ingredientOptions().filter(([id, label]) => id === value || `${id} ${label}`.toLocaleLowerCase().includes(filter))], value); });
  const quantityLabel = node('label', 'Amount'); quantityLabel.append(amount);
  const unitLabel = node('label', 'Unit'); unitLabel.append(unit);
  select.addEventListener('change', syncPantry);
  const remove = node('button', '×', 'quiet'); remove.type = 'button'; remove.setAttribute('aria-label', t('Remove pantry ingredient'));
  remove.addEventListener('click', () => { row.remove(); syncPantry(); }); row.append(quantity, ingredientLabel, searchLabel, quantityLabel, unitLabel, remove);
  $('pantry-rows').append(row);
  syncPantry();
  return row;
}
function configurePantry() {
  if ($('condiment-options')) {
    $('condiment-options').replaceChildren();
    for (const id of capabilities.condiment_ids ?? ['salt', 'black_pepper', 'paprika', 'dried_herbs', 'cumin', 'chilli', 'garlic_powder']) {
      const label = node('label', undefined, 'choice-chip'), input = node('input'); input.type = 'checkbox'; input.value = id; input.id = `condiment-${id}`;
      input.disabled = !capabilities.recipe_request_schema.properties.condiments;
      label.append(input, node('span', capabilities.condiment_labels?.[id]?.[language.current] ?? t(id), undefined, true)); $('condiment-options').append(label);
    }
  }
  renderQuickAdd();
}
function renderQuickAdd() {
  if ($('pantry-quick-add')) {
    $('pantry-quick-add').replaceChildren();
    for (const id of ['oil', 'rice', 'chicken', 'chicken_breast', 'onion'].filter(id => capabilities.ingredients.includes(id))) {
      const button = node('button', t('Add {ingredient}', {ingredient: ingredientOptions().find(([value]) => value === id)[1]}), 'quiet'); button.type = 'button';
      button.addEventListener('click', () => {
        const existing = [...$('pantry-rows').children].find(row => row.querySelector('select').value === id);
        const row = existing ?? addPantry(id); row.querySelector('[data-pantry-amount]').focus();
      }); $('pantry-quick-add').append(button);
    }
  }
}
async function activateSession(token) {
  if (token !== undefined) api.setSession({token, session_mode: 'bearer'});
  const active = ++generation;
  let user, supported;
  try { [user, supported] = await Promise.all([api.me(), api.capabilities()]); }
  catch (error) { if (active === generation) { endSession(); notice(errorMessage(error)); } return; }
  if (active !== generation) return;
  capabilities = supported; $('username').textContent = user.username;
  $('signin-panel').hidden = true; $('account').hidden = false; $('workspace').hidden = false;
  $('session-token').value = ''; $('invite-token').value = '';
  clearPasswords(); registrationError('');
  $('coverage').textContent = `${supported.sources.join(', ')} · ${supported.location_label ?? supported.scope}`;
  options($('provider'), supported.providers.map(id => [id, id === 'template' ? 'Configured recipes' : id]), supported.default_provider);
  options($('cache-policy'), supported.cache_policies.map(id => [id, id === 'cache-only' ? 'Saved grocery data only' : id]), supported.default_cache_policy);
  options($('meal-style'), supported.meal_styles.map(id => [id, t(id)]), supported.defaults?.meal_style);
  options($('exclusions'), ingredientOptions());
  options($('use-first'), ingredientOptions());
  options($('recipe-sources'), supported.sources.map(source => [source, source]), supported.sources[0]);
  $('recipe-sources').multiple = Boolean(supported.combined_sources);
  options($('offer-source'), supported.sources.map(source => [source, source]), supported.sources[0]);
  const properties = supported.recipe_request_schema.properties;
  if ($('cuisine')) { $('cuisine').value = supported.defaults?.cuisine ?? ''; renderCuisines(); }
  if ($('recipe-count')) { $('recipe-count').disabled = !properties.recipe_count; $('recipe-count').value = 3; $('recipe-count').max = supported.max_recipe_count ?? 6; }
  configurePantry();
  for (const [id, field] of [['min-protein', 'min_protein_g'], ['max-kcal', 'max_kcal'], ['max-minutes', 'max_minutes'], ['recipe-retailers', 'retailer_ids'], ['recipe-loyalty', 'allow_loyalty'], ['use-first', 'use_first'], ['have-seasonings', 'seasonings_available']]) $(id).disabled = !properties[field];
  for (const [id, key] of [['servings', 'servings'], ['max-stores', 'max_stores']]) {
    $(id).min = properties[key].minimum; $(id).max = properties[key].maximum;
    $(id).value = supported.defaults?.[key] ?? properties[key].default; $(id).step = '1';
  }
  options($('offer-unit'), [['', 'Any unit'], ...supported.offer_query_schema.$defs.Unit.enum.map(unit => [unit, unit])]);
  adminAllowed = user.role === 'admin'; $('admin').hidden = $('admin-link').hidden = !adminAllowed; $('refresh-catalogue').disabled = !supported.refresh_allowed;
  options($('refresh-source'), supported.sources.map(source => [source, source])); refreshProfiles();
  $('pantry-rows').replaceChildren(); addPantry(); setModel();
  currentJob = undefined; result = undefined; offerOffset = jobsOffset = 0;
  const empty = node('div', undefined, 'empty-state');
  const marker = node('span', '◌'); marker.setAttribute('aria-hidden', 'true');
  empty.append(marker, node('h3', 'Your next meal starts here'), node('p', 'Choose your servings and pantry ingredients, then find a recipe. Your plan will include nutrition, a shopping list, and cooking steps.'));
  $('result').replaceChildren(empty);
  $('job-progress').textContent = t('Ready when you are.');
  $('recipe-language').value = supported.defaults?.language ?? 'en';
  for (const [id, field] of [['budget', 'max_cost_per_serving_czk'], ['min-protein', 'min_protein_g'], ['max-kcal', 'max_kcal'], ['max-minutes', 'max_minutes']]) {
    const value = supported.defaults?.[field]; if (value != null) $(id).placeholder = displayDecimal(value);
  }
  for (const [id, field] of [['recipe-loyalty', 'allow_loyalty'], ['have-seasonings', 'seasonings_available']]) $(id).options && ($(id).options[0].textContent = t('Configured preference') + (supported.defaults?.[field] != null ? ` · ${t(supported.defaults[field] ? 'Yes' : 'No')}` : ''));
  for (const choice of choices.splice(0)) choice.destroy();
  for (const id of ['exclusions', 'use-first', 'recipe-sources']) choices.push(choiceList($(id), t));
  for (const id of ['recipe-retailers', 'offer-retailer', 'account-retailers']) if ($(id) && supported.retailer_ids?.length) choices.push(retailerChoices($(id), supported.retailer_ids, t));
  configureScope();
  await accountUI.load();
  if (active !== generation) return;
  relabel();
  routeScreen();
  notice(''); await loadJobs();
  if (active === generation && supported.saved_recipes_enabled === true) await loadBookmarks();
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
  const source = $('refresh-source').value, profiles = capabilities.refresh_profiles?.[source] ?? [];
  options($('refresh-profile'), profiles.length ? profiles.map(id => [id, capabilities.profile_metadata?.[source]?.find(item => item.fingerprint === id)?.name ?? id]) : [['', 'Configured default']]);
}
function showJob(job) {
  currentJob = job; $('current-job').hidden = false; $('job-reference').textContent = t('Request {id}', {id: job.job_id});
  $('job-progress').textContent = t('Request state: {status} · Phase: {phase}', {status: t(job.status), phase: t(job.phase)});
  $('cancel-job').hidden = !['queued', 'running'].includes(job.status);
  if (['queued', 'running'].includes(job.status) && !result) {
    $('result').replaceChildren(node('p', job.status === 'queued' ? 'Your request is queued. Recipes will appear here when it finishes.' : 'Your request is running. You can keep planning while we check for results.', 'hint'));
  }
}
async function selectJob(job, preserveResult = false) {
  const selected = ++selection;
  if (['#jobs', '#offers', '#account-settings', '#pantry', '#admin', '#bookmarks'].includes(window.location?.hash)) window.location.hash = 'recipes';
  clearTimeout(timer);
  if (!preserveResult) { result = displayedJob = displayedRequest = undefined; $('downloads').hidden = true; $('result').replaceChildren(); }
  summaryRequest = undefined; $('request-summary')?.remove();
  showJob(job);
  const active = generation;
  if (job.kind !== 'refresh') {
    try { const request = await api.jobRequest(job.job_id); if (active === generation && selected === selection && currentJob?.job_id === job.job_id) { summaryRequest = request; renderSummary(); } }
    catch (error) { if (active !== generation) return; if (error.status === 401) throw error; }
  }
  if (active === generation && selected === selection) await checkJob();
}
async function checkJob() {
  if (!currentJob) return;
  clearTimeout(timer); const id = currentJob.job_id, active = generation, selected = selection;
  const job = await api.job(id);
  if (active !== generation || selected !== selection || currentJob?.job_id !== id) return;
  showJob(job);
  if (['queued', 'running'].includes(job.status)) {
    timer = setTimeout(() => run(checkJob), 1500); return;
  }
  if (job.status === 'succeeded') {
    const completed = await api.result(id);
    if (active !== generation || selected !== selection || currentJob?.job_id !== id) return;
    result = completed; displayedJob = job; displayedRequest = summaryRequest; renderResult(completed, job.kind);
    $('downloads').hidden = job.kind !== 'recipe';
  } else {
    const message = job.status === 'cancelled' ? 'This request was cancelled.' : t('This request failed: {code}. You can submit a new request.', {code: job.error_code ?? t('execution failed')});
    if (result) notice(message); else $('result').replaceChildren(node('p', message, 'warning'));
  }
  await loadJobs();
}
function renderResult(value, kind) {
  const target = $('result');
  const sameJob = displayedJob && target.dataset.jobId === displayedJob.job_id;
  const expanded = sameJob ? new Set([...target.querySelectorAll('.recipe-card')].filter(card => card.open).map(card => Number(card.dataset.mealIndex))) : undefined;
  const focusedCard = document.activeElement?.closest?.('.recipe-card');
  const focusedIndex = sameJob && focusedCard ? Number(focusedCard.dataset.mealIndex) : undefined;
  const focusedBookmark = document.activeElement?.dataset?.bookmarkKey;
  target.dataset.jobId = displayedJob?.job_id ?? ''; target.replaceChildren();
  if (displayedJob) target.append(node('p', t('Recipes from request {id}', {id: displayedJob.job_id}), 'hint'));
  if (kind === 'refresh') { target.append(node('p', t('Grocery refresh completed. Run {id}.', {id: value.run_id}))); return; }
  if (value.status === 'no-feasible-recipe') {
    const reasons = {'no-template-after-exclusions': 'No recipe remains after ingredient exclusions.', 'no-complete-recipe-within-limits': 'No complete recipe meets your nutrition, budget and shopping limits.'};
    target.append(node('p', reasons[value.reason_code] ?? 'No recipe meets your preferences.', 'warning'));
    if (value.reason) { const details = node('details', undefined, 'technical-warnings'); details.append(node('summary', 'Technical details'), node('p', value.reason, undefined, true)); target.append(details); }
    return;
  }
  const report = value.report;
  if (!report) { target.append(node('p', 'No recipe meets your preferences.', 'warning')); return; }
  if (value.batch?.failures?.length || value.partial_errors?.length || value.errors?.length || value.batch?.errors?.length || value.status === 'partial' || value.status === 'partial-success') {
    target.append(node('p', 'Some recipes could not be generated. Your completed recipes are available below. Try a new request for more recipes.', 'warning'));
  }
  if (report.warnings?.length || report.warning_codes?.length) { const warnings = node('div'); target.append(warnings); accountUI.warnings(warnings, report); }
  target.append(node('p', t('Grocery data from {date}.', {date: date(report.batch_finished_at)}), 'hint'));
  for (const [index, meal] of report.meals.entries()) {
    const card = node('details', undefined, 'recipe-card'); card.open = expanded ? expanded.has(index) : index === 0;
    card.dataset.mealIndex = index;
    const summary = node('summary');
    summary.append(node('h3', meal.title, undefined, true), node('p', t('{minutes} minutes · {servings} servings · {stores}', {minutes: meal.minutes, servings: meal.servings, stores: meal.stores.length ? meal.stores.join(', ') : t('From your pantry')})));
    card.append(summary);
    if (capabilities.saved_recipes_enabled === true && displayedJob) {
      const jobId = displayedJob.job_id, key = `${jobId}:${index}`;
      const save = node('button', bookmarkedMeals.has(key) ? 'Bookmarked' : 'Bookmark recipe', 'quiet'); save.type = 'button'; save.dataset.bookmarkKey = key; save.setAttribute('aria-disabled', String(bookmarkedMeals.has(key) || bookmarkBusy.has(key)));
      save.addEventListener('click', () => run(async () => {
        if (bookmarkBusy.has(key) || bookmarkedMeals.has(key)) return;
        const active = generation; bookmarkBusy.add(key); updateBookmarkButtons();
        try {
          const bookmark = await api.saveRecipe(jobId, index);
          if (active !== generation) return;
          bookmarkedMeals.set(key, bookmark.id); await loadBookmarks();
        } finally { if (active === generation) { bookmarkBusy.delete(key); updateBookmarkButtons(); } }
      })); card.append(save);
    }
    const metrics = node('div', undefined, 'metrics');
    for (const label of [t('{cost} Kč / serving', {cost: displayDecimal(meal.usage_cost_per_serving_czk)}), t('{grams} g protein', {grams: displayDecimal(meal.nutrients_per_serving.protein_g)}), `${displayDecimal(meal.nutrients_per_serving.kcal)} kcal`]) metrics.append(node('span', label, 'metric'));
    card.append(metrics, node('h4', 'Ingredients & shopping'));
    const ingredients = node('ul');
    for (const line of meal.lines) {
      const price = line.price, label = capabilities.ingredient_localized_labels?.[price.ingredient_id]?.[language.current] ?? price.label;
      const item = node('li', t('{label}: {required} g total · {owned} g owned · {buy} g to buy', {label, required: displayDecimal(line.required_grams), owned: displayDecimal(line.owned_grams), buy: displayDecimal(line.purchased_grams)}));
      item.append(node('span', `${t('Retailer')}: ${price.offer?.product?.store_id ?? t('From your pantry')} · ${t('Source')}: ${price.source_id ?? t('From your pantry')} · ${t('Shopping context')}: ${price.shopping_context || t('From your pantry')}`, 'hint'));
      if (price.offer?.source_url && Number(line.purchased_grams) > 0) { const link = node('a', 'View source'); if (safeLink(link, price.offer.source_url)) item.append(link); }
      ingredients.append(item);
    }
    card.append(ingredients, node('h4', 'Preparation'));
    const steps = node('ol'); for (const step of meal.steps) steps.append(node('li', step, undefined, true)); card.append(steps);
    if (displayedJob && capabilities.recipe_export_formats?.some(format => ['text', 'ingredients', 'schema-org'].includes(format))) card.append(recipeExportControls(displayedJob.job_id, index));
    target.append(card);
  }
  target.append(node('p', 'Nutrition is an estimate. Costs reflect ingredients used, not complete packages or checkout charges.', 'hint'));
  if (focusedIndex !== undefined) {
    const card = [...target.querySelectorAll('.recipe-card')].find(card => Number(card.dataset.mealIndex) === focusedIndex);
    const control = focusedBookmark ? card?.querySelector('[data-bookmark-key]') : card?.querySelector('summary');
    control?.focus({preventScroll: true});
  }
}
function recipeExportControls(jobId, index) {
  const active = generation, uiLanguage = language.current;
  const disclosure = node('details', undefined, 'recipe-export'); disclosure.append(node('summary', 'Export to a nutrition app'));
  const fields = node('div', undefined, 'field-grid');
  const format = node('select'), locale = node('select');
  format.dataset.exportFormat = ''; locale.dataset.exportLanguage = '';
  format.id = `recipe-export-format-${index}`; locale.id = `recipe-export-language-${index}`;
  options(format, [['text', 'Full text · Kalorické Tabulky (manual)'], ['ingredients', 'Ingredients · Cronometer Gold / MyFitnessPal'], ['schema-org', 'schema.org JSON · Mealie']].filter(([id]) => capabilities.recipe_export_formats.includes(id)));
  options(locale, [['cs', 'Čeština'], ['en', 'English']]); locale.value = uiLanguage;
  for (const [control, title] of [[format, 'Export format'], [locale, 'Export language']]) {
    const field = node('div'), label = node('label', title); label.htmlFor = control.id; field.append(label, control); fields.append(field);
  }
  const note = node('p', 'Kalorické Tabulky has no verified file importer here: enter foods manually. Review matched ingredients in other apps. Weights are raw edible amounts for all servings; nutrition is estimated per serving. Cooked mass is unknown.', 'hint');
  const actions = node('div', undefined, 'export-actions'), status = node('p', undefined, 'hint'); status.setAttribute('role', 'status');
  const previewLabel = node('label', 'Export preview'), preview = node('textarea');
  preview.id = `recipe-export-preview-${index}`; preview.dataset.exportPreview = ''; preview.readOnly = true; preview.rows = 8;
  previewLabel.htmlFor = preview.id; previewLabel.hidden = preview.hidden = true;
  let revision = 0;
  for (const control of [format, locale]) control.addEventListener('change', () => { revision += 1; preview.value = ''; previewLabel.hidden = preview.hidden = true; status.textContent = ''; });
  for (const action of ['Download export', 'Copy export']) {
    const button = node('button', action, 'secondary'); button.type = 'button'; button.dataset.exportAction = action === 'Copy export' ? 'copy' : 'download';
    let busy = false;
    button.addEventListener('click', async () => {
      if (busy) return;
      const selected = revision, selectedFormat = format.value, selectedLanguage = locale.value;
      const valid = () => active === generation && uiLanguage === language.current && selected === revision && $('result').contains(disclosure) && displayedJob?.job_id === jobId;
      if (!valid()) return;
      busy = true; button.disabled = true; button.setAttribute('aria-busy', 'true'); status.textContent = t('Preparing export…');
      try {
        const body = await api.recipeExport(jobId, index, selectedFormat, selectedLanguage);
        if (!valid()) return;
        preview.value = body; previewLabel.hidden = preview.hidden = false;
        if (action === 'Download export') {
          const safeId = String(jobId).replace(/[^a-zA-Z0-9_-]/g, '_');
          download(body, selectedFormat === 'schema-org' ? 'application/ld+json' : 'text/plain;charset=utf-8', `recipe-${safeId}-${index}-${selectedLanguage}-${selectedFormat}.${selectedFormat === 'schema-org' ? 'json' : 'txt'}`);
          status.textContent = t('Export downloaded. Review food matches before logging.');
        } else {
          try {
            await navigator.clipboard.writeText(body);
            if (valid()) status.textContent = t('Export copied. Review food matches before logging.');
          } catch {
            if (!valid()) return;
            status.textContent = t('Clipboard unavailable. Select and copy the preview below.'); preview.focus(); preview.select();
          }
        }
      } catch (error) {
        if (!valid()) return;
        if (error.status === 401) { endSession(); notice(errorMessage(error)); }
        else status.textContent = errorMessage(error);
      } finally {
        busy = false; button.disabled = false; button.removeAttribute('aria-busy');
      }
    }); actions.append(button);
  }
  disclosure.append(fields, note, actions, status, previewLabel, preview); return disclosure;
}
function updateBookmarkButtons() {
  for (const button of $('result').querySelectorAll('[data-bookmark-key]')) {
    const key = button.dataset.bookmarkKey, saved = bookmarkedMeals.has(key);
    button.setAttribute('aria-disabled', String(saved || bookmarkBusy.has(key)));
    button.dataset.i18n = saved ? 'Bookmarked' : 'Bookmark recipe'; button.textContent = t(button.dataset.i18n);
  }
}
function pagination(prefix, total, offset, limit) {
  $(prefix + '-prev').disabled = offset === 0;
  $(prefix + '-next').disabled = offset + limit >= total;
  $(prefix + '-count').textContent = total ? t('{start}–{end} of {total}', {start: offset + 1, end: Math.min(offset + limit, total), total}) : t('No results');
}
async function loadBookmarks() {
  if (!capabilities?.saved_recipes_enabled) return;
  const active = generation, offset = bookmarkOffset, loading = ++bookmarkLoad;
  const page = await api.savedRecipes(offset, pageSize);
  if (active !== generation || offset !== bookmarkOffset || loading !== bookmarkLoad) return;
  if (offset && !page.items.length) { bookmarkOffset = Math.max(0, offset - pageSize); return loadBookmarks(); }
  for (const item of page.items) bookmarkedMeals.set(`${item.job_id}:${item.meal_index}`, item.id);
  lastBookmarks = page; renderBookmarks(page);
  updateBookmarkButtons();
}
function renderBookmarks(page) {
  $('bookmark-list').replaceChildren(); $('bookmarks-status').textContent = page.total ? '' : t('No bookmarked recipes yet.');
  for (const item of page.items) {
    const row = node('li'); row.append(node('span', `${item.title ?? item.meal?.title ?? t('Recipe')} · ${date(item.created_at)}`, undefined, true));
    for (const action of ['Open', 'Reuse preferences', 'Remove']) {
      const button = node('button', action, 'quiet'); button.type = 'button';
      button.addEventListener('click', () => run(async () => {
        const active = generation;
        if (action === 'Remove') {
          button.disabled = true;
          try { await api.removeSavedRecipe(item.id); if (active !== generation) return; bookmarkedMeals.delete(`${item.job_id}:${item.meal_index}`); await loadBookmarks(); }
          finally { if (active === generation) button.disabled = false; }
          return;
        }
        const selected = ++selection;
        const detail = await api.savedRecipe(item.id); if (active !== generation || selected !== selection) return;
        if (action === 'Reuse preferences') accountUI.applyParameters(detail.request);
        else {
          clearTimeout(timer);
          currentJob = displayedJob = {job_id: detail.job_id, kind: 'recipe', status: 'succeeded', phase: 'completed'};
          result = detail.result; summaryRequest = displayedRequest = detail.request;
          showJob(currentJob); renderSummary(); renderResult(result, 'recipe'); $('downloads').hidden = false;
          for (const card of $('result').querySelectorAll('.recipe-card')) card.open = Number(card.dataset.mealIndex) === detail.meal_index;
          const card = [...$('result').querySelectorAll('.recipe-card')].find(card => Number(card.dataset.mealIndex) === detail.meal_index);
          card?.querySelector('summary')?.setAttribute('tabindex', '-1');
        }
        window.location.hash = 'recipes'; routeScreen();
        if (action === 'Open') [...$('result').querySelectorAll('.recipe-card')].find(card => Number(card.dataset.mealIndex) === detail.meal_index)?.querySelector('summary')?.focus();
        if (action === 'Reuse preferences') $('meal-style').focus();
      })); row.append(button);
    }
    $('bookmark-list').append(row);
  }
  pagination('bookmarks', page.total, page.offset, page.limit);
}
async function loadJobs() {
  const active = generation; const page = await api.jobs(jobsOffset, pageSize);
  if (active !== generation) return;
  lastJobs = page; renderJobs(page);
}
function renderJobs(page) {
  $('job-list').replaceChildren();
  if (!jobsEmpty) { jobsEmpty = node('p', undefined, 'hint'); $('job-list').after(jobsEmpty); }
  jobsEmpty.hidden = page.items.length > 0;
  jobsEmpty.textContent = t(page.total ? 'No requests on this page. Go back to see earlier requests.' : 'No requests yet. Choose your meal and pantry ingredients to create your first recipe request.');
  for (const job of page.items) {
    const item = node('li'); item.dataset.jobId = job.job_id; item.append(node('span', `${t(job.kind === 'refresh' ? 'Grocery refresh' : 'Recipe')} · ${t(job.status)} · ${date(job.created_at)} · ${t('Request {id}', {id: job.job_id})}`));
    const open = node('button', 'Open', 'quiet'); open.addEventListener('click', () => run(() => selectJob(job))); item.append(open); $('job-list').append(item);
    if (job.kind === 'recipe') {
      const reuse = node('button', 'Reuse preferences', 'quiet'); reuse.addEventListener('click', () => run(async () => {
        const active = generation; const request = await api.jobRequest(job.job_id); if (active !== generation) return;
        accountUI.applyParameters(request); if (window.location) window.location.hash = 'recipes'; $('meal-style').focus();
      })); item.append(reuse);
    }
  }
  pagination('jobs', page.total, page.offset, page.limit);
}
async function loadOffers() {
  const active = generation;
  const source = $('offer-source').value;
  const page = await api.offers({...offerParameters, source, ...(!offerParameters.scope && !offerParameters.profile_fingerprint ? {scope: capabilities.source_scopes?.[source]?.[0] ?? capabilities.scope} : {}), offset: offerOffset, limit: pageSize});
  if (active !== generation) return;
  lastOffers = page; renderOffers(page);
}
function renderOffers(page) {
  const state = page.state;
  $('offer-status').textContent = t('{date} · fresh until {until}', {date: date(state.batch_finished_at), until: date(state.fresh_until)});
  accountUI.warnings($('offer-status'), state);
  $('offer-rows').replaceChildren();
  for (const {offer, observed_at} of page.items) {
    const row = node('tr'); const product = node('td');
    const link = node('a', offer.product.name, undefined, true);
    safeLink(link, offer.source_url);
    product.append(link, node('span', offer.scope, 'hint'));
    if (offer.promotion?.conditions) product.append(node('span', offer.promotion.conditions, 'hint'));
    if (offer.promotion?.requires_loyalty) product.append(node('span', 'Loyalty price', 'hint'));
    row.append(product, node('td', offer.product.store_id), node('td', `${offer.price_qualifier === 'from' ? t('From') + ' ' : ''}${displayDecimal(offer.current_price)} ${offer.currency} / ${displayDecimal(offer.price_basis.amount)} ${offer.price_basis.unit}`), node('td', `${displayDecimal(offer.unit_price.amount)} ${offer.currency} / ${offer.unit_price.unit}`), node('td', date(observed_at)));
    $('offer-rows').append(row);
  }
  pagination('offers', page.total, page.offset, page.limit);
}
function download(body, type, filename) {
  const url = URL.createObjectURL(new Blob([body], {type})); const link = node('a');
  link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function date(value) { return new Date(value).toLocaleString(language.current === 'cs' ? 'cs-CZ' : 'en-GB', {timeZone: capabilities?.read_only_policy?.timezone}); }
function safeLink(link, value) { try { const url = new URL(value); if (['http:', 'https:'].includes(url.protocol)) { link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; return true; } } catch { /* Invalid source URLs stay inert. */ } return false; }
function configureScope() {
  const scopes = capabilities.source_scopes?.[$('offer-source').value] ?? [capabilities.scope];
  options($('offer-scope'), [['', 'Configured scope'], ...scopes.filter(Boolean).map(value => [value, value])]);
}
function relabel() {
  translateStatic();
  if (document.documentElement) document.documentElement.lang = language.current;
  document.title = t('Grocery Agent · Your next meal'); $('ui-language').value = language.current;
  for (const element of document.querySelectorAll('[data-i18n]')) element.textContent = t(element.dataset.i18n);
  const register = !$('register-form').hidden;
  $('signin-heading').textContent = t(register ? 'Create your account' : 'Welcome back');
  $('signin-description').textContent = t(register ? 'Choose a username and password to plan meals and keep your reports together.' : 'Plan from your pantry and pick up your meal reports.');
  if (!capabilities) return;
  for (const id of ['exclusions', 'use-first']) for (const option of $(id).options ?? []) option.textContent = ingredientOptions().find(([value]) => value === option.value)?.[1] ?? option.value;
  for (const row of $('pantry-rows').children) {
    const select = row.querySelector('select'), value = select.value;
    options(select, [['', 'Choose ingredient'], ...ingredientOptions()], value);
    row.querySelector('button')?.setAttribute('aria-label', t('Remove pantry ingredient'));
  }
  for (const choice of choices) choice.render();
  renderCuisines();
  renderQuickAdd();
  for (const input of $('condiment-options')?.querySelectorAll('input') ?? []) input.nextElementSibling.textContent = capabilities.condiment_labels?.[input.value]?.[language.current] ?? t(input.value);
  for (const row of $('pantry-rows').children) {
    const unit = row.querySelector('[data-pantry-unit]'); if (unit) options(unit, [['g', 'Grams'], ['kg', 'Kilograms'], ['available', 'Enough available']], unit.value);
  }
  accountUI.render();
  if (lastJobs) renderJobs(lastJobs); if (lastOffers) renderOffers(lastOffers);
  if (lastBookmarks) renderBookmarks(lastBookmarks);
  if (result && displayedJob) renderResult(result, displayedJob.kind);
  if (currentJob) { $('job-reference').textContent = t('Request {id}', {id: currentJob.job_id}); $('job-progress').textContent = t('Request state: {status} · Phase: {phase}', {status: t(currentJob.status), phase: t(currentJob.phase)}); }
  for (const [id, field] of [['budget', 'max_cost_per_serving_czk'], ['min-protein', 'min_protein_g'], ['max-kcal', 'max_kcal'], ['max-minutes', 'max_minutes']]) if (capabilities.defaults?.[field] != null) $(id).placeholder = displayDecimal(capabilities.defaults[field]);
  renderSummary();
}
function renderSummary() {
  $('request-summary')?.remove();
  if (!summaryRequest) return;
  const details = node('details', undefined, 'preferences'); details.id = 'request-summary'; details.open = true;
  details.append(node('summary', 'Request summary'));
  const list = node('dl');
  for (const [field, value] of Object.entries(summaryRequest)) {
    if (value == null) continue;
    let display = typeof value === 'boolean' ? t(value ? 'Yes' : 'No') : Array.isArray(value) ? value.map(item => capabilities.ingredient_localized_labels?.[item]?.[language.current] ?? capabilities.condiment_labels?.[item]?.[language.current] ?? t(item)).join(', ') : typeof value === 'object' ? Object.entries(value).map(([id, amount]) => `${capabilities.ingredient_localized_labels?.[id]?.[language.current] ?? id}: ${amount}`).join(', ') : t(String(value));
    if (field === 'cuisine') display = cuisineLabel(value);
    if (['max_cost_per_serving_czk', 'min_protein_g', 'max_kcal'].includes(field)) display = displayDecimal(display);
    list.append(node('dt', fieldLabels[field] ?? field), node('dd', display));
  }
  details.append(list); $('current-job').append(details);
}
function routeScreen() {
  const hash = window.location?.hash?.slice(1), screens = {recipes: 'recipe-screen', pantry: 'pantry', offers: 'offers', bookmarks: 'bookmarks', jobs: 'jobs', 'account-settings': 'account-settings', admin: 'admin'};
  const selected = Object.hasOwn(screens, hash ?? '') ? hash : 'recipes';
  for (const [route, id] of Object.entries(screens)) $(id).hidden = (route === 'admin' && !adminAllowed) || Boolean(selected && selected !== route);
  for (const link of document.querySelectorAll('.tabs a')) {
    if (link.getAttribute('href') === `#${selected}`) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
}
window.addEventListener('hashchange', routeScreen);
function validationError(error) {
  showValidation(error, document, t, control => {
    const screen = control.closest('#pantry') ? 'pantry' : control.closest('#account-settings') ? 'account-settings' : 'recipes';
    if (window.location) window.location.hash = screen; routeScreen();
  });
}
for (const event of ['input', 'change']) window.addEventListener(event, event => { if (event.target?.dataset?.validationError) clearValidation(document, event.target); });
$('ui-language').addEventListener('change', () => { language.choose($('ui-language').value); relabel(); if (capabilities) accountUI.syncLanguage(language.current); });
relabel();

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
$('cuisine')?.addEventListener('change', () => { updateCuisineControls(); if ($('cuisine').value) accountUI.suggestPresetName(cuisineLabel($('cuisine').value)); });
$('recipe-sources').addEventListener('change', sourceProfiles);
$('offer-source').addEventListener('change', () => { options($('offer-profile'), profileEntries($('offer-source').value)); configureScope(); lastOffers = undefined; offerParameters = {}; offerOffset = 0; $('offer-rows').replaceChildren(); pagination('offers', 0, 0, pageSize); });
$('add-pantry').addEventListener('click', addPantry);
function readRecipe(includePantry = true) {
  clearValidation(document);
  const pantry = includePantry ? [...$('pantry-rows').children].map(row => ({ingredient: row.querySelector('select').value, quantity: row.querySelector('input').value.trim()})) : [];
  const body = recipePayload({provider: $('provider').value, model: $('model').disabled ? '' : $('model').value, cache_policy: $('cache-policy').value, meal_style: $('meal-style').value, servings: $('servings').value, max_stores: $('max-stores').value, budget: decimal($('budget').value), exclusions: [...$('exclusions').selectedOptions].map(option => option.value), language: capabilities.recipe_request_schema?.properties?.language ? $('recipe-language').value : undefined}, capabilities, pantry);
  if (capabilities.recipe_request_schema.properties.condiments) body.condiments = accountUI.getCondiments();
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
    cuisine: $('cuisine')?.disabled ? undefined : ($('cuisine')?.value || null),
    recipe_count: capabilities.recipe_request_schema.properties.recipe_count ? $('recipe-count').value : undefined,
    min_protein_g: $('min-protein').disabled ? '' : decimal($('min-protein').value), max_kcal: $('max-kcal').disabled ? '' : decimal($('max-kcal').value), max_minutes: $('max-minutes').disabled ? '' : $('max-minutes').value,
    retailer_ids: $('recipe-retailers').disabled || !$('recipe-retailers').value.trim() ? [] : $('recipe-retailers').value.split(',').map(id => id.trim()),
    allow_loyalty: $('recipe-loyalty').disabled || !$('recipe-loyalty').value ? undefined : $('recipe-loyalty').value === 'true', use_first: !includePantry || $('use-first').disabled ? [] : [...$('use-first').selectedOptions].map(option => option.value), seasonings_available: $('have-seasonings').disabled || !$('have-seasonings').value ? undefined : $('have-seasonings').value === 'true'}, capabilities, pantry);
  for (const field of ['cuisine', 'recipe_count', 'min_protein_g', 'max_kcal', 'max_minutes', 'retailer_ids', 'allow_loyalty', 'use_first']) if (Object.hasOwn(extra, field)) body[field] = extra[field];
  return body;
}
$('recipe-form').addEventListener('submit', event => { event.preventDefault(); run(async () => {
  if (recipeBusy?.generation === generation) return;
  const body = readRecipe();
  const serialized = JSON.stringify(body); if (pendingSubmission?.serialized !== serialized) pendingSubmission = {serialized, key: crypto.randomUUID()};
  const active = generation, submission = {generation: active}; recipeBusy = submission; $('generate').disabled = true; $('recipe-form').setAttribute('aria-busy', 'true'); notice('');
  try { const job = await api.submit(body, pendingSubmission.key); if (active !== generation) return;
    pendingSubmission = undefined; await selectJob(job, true); await loadJobs();
  } finally {
    if (recipeBusy === submission) { recipeBusy = undefined; $('recipe-form').removeAttribute('aria-busy'); if (active === generation) setModel(); }
  }
}); });
$('check-job').addEventListener('click', () => run(checkJob));
$('cancel-job').addEventListener('click', () => run(async () => { const id = currentJob.job_id; await api.cancel(id); if (currentJob?.job_id === id) await checkJob(); }));
$('download-html').addEventListener('click', () => run(async () => { const id = displayedJob.job_id, active = generation; const html = await api.report(id); if (active === generation) download(html, 'text/html', `recipe-${id}.html`); }));
$('download-json').addEventListener('click', () => { if (result && displayedJob) download(JSON.stringify(result, null, 2), 'application/json', `recipe-${displayedJob.job_id}.json`); });
$('reload-bookmarks')?.addEventListener('click', () => run(loadBookmarks));
for (const [id, delta] of [['bookmarks-prev', -pageSize], ['bookmarks-next', pageSize]]) $(id)?.addEventListener('click', () => run(async () => { bookmarkOffset = Math.max(0, bookmarkOffset + delta); await loadBookmarks(); }));
$('offers-form').addEventListener('submit', event => { event.preventDefault(); run(async () => {
  offerParameters = {sort: $('offer-sort').value, allow_loyalty: $('offer-loyalty').checked, include_from: $('offer-from').checked, include_unavailable: $('offer-unavailable').checked};
  if ($('offer-profile').value) offerParameters.profile_fingerprint = $('offer-profile').value;
  for (const [key, id] of [['search', 'offer-search'], ['unit', 'offer-unit'], ['category', 'offer-category'], ['scope', 'offer-scope']]) if ($(id).value.trim()) offerParameters[key] = $(id).value.trim();
  if ($('offer-retailer').value.trim()) offerParameters.retailer = $('offer-retailer').value.split(',').map(value => value.trim()).filter(Boolean);
  if ($('offer-max-price').value.trim()) { const amount = decimal($('offer-max-price').value); if (!/^\d+(?:\.\d+)?$/.test(amount)) throw new Error('Budget must be a positive decimal amount.'); offerParameters.max_unit_price = amount; }
  if ((offerParameters.sort === 'unit_price' || offerParameters.max_unit_price) && !offerParameters.unit) throw new Error('Choose a comparable unit before sorting by unit price.');
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

// Shared website transport source. Credentials are kept only in this instance.
export class ApiError extends Error {
  constructor(status, detail) { super(typeof detail === 'string' ? detail : JSON.stringify(detail)); this.status = status; }
}

export class GroceryClient {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) { this.fetcher = fetcher; this.clearSession(); }
  clearSession() { this.token = ''; this.csrf = ''; this.cookieMode = false; }
  setSession(response) { this.clearSession(); this.token = response.token ?? ''; this.csrf = response.csrf_token ?? ''; this.cookieMode = response.session_mode === 'cookie'; }
  async request(path, {method = 'GET', body, key, text = false, signal} = {}) {
    const headers = new Headers();
    if (this.token) headers.set('Authorization', `Bearer ${this.token}`);
    if (this.cookieMode && !['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRF-Token', this.csrf);
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (key) headers.set('Idempotency-Key', key);
    const response = await this.fetcher(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal, redirect: 'error', credentials: this.cookieMode ? 'same-origin' : 'omit', cache: 'no-store'});
    if (!response.ok) {
      let detail = 'Could not complete this request.';
      try { detail = (await response.json()).detail ?? detail; } catch { /* Response may not be JSON. */ }
      throw new ApiError(response.status, detail);
    }
    if (response.status === 204) return null;
    return text ? response.text() : response.json();
  }
  me() { return this.request('/v1/me'); }
  authCapabilities() { return this.request('/v1/auth/capabilities'); }
  async resume() { this.clearSession(); this.cookieMode = true; try { const response = await this.request('/v1/auth/session'); this.setSession(response); return response; } catch (error) { this.clearSession(); throw error; } }
  async login(username, password, session_mode = 'bearer') { this.clearSession(); this.cookieMode = session_mode === 'cookie'; try { const response = await this.request('/v1/auth/login', {method: 'POST', body: {username, password, session_mode}}); this.setSession(response); return response; } catch (error) { this.clearSession(); throw error; } }
  capabilities() { return this.request('/v1/capabilities'); }
  collections(source_id) { return this.request(`/v1/collections?${new URLSearchParams({source: source_id})}`); }
  async acceptInvitation(token, password, session_mode = 'bearer') { this.clearSession(); this.cookieMode = session_mode === 'cookie'; try { const response = await this.request('/v1/invitations/accept', {method: 'POST', body: {token, ...(password ? {password} : {}), session_mode}}); this.setSession(response); return response; } catch (error) { this.clearSession(); throw error; } }
  invite(username) { return this.request('/v1/invitations', {method: 'POST', body: {username}}); }
  submit(body, key) { return this.request('/v1/recipes', {method: 'POST', body, key}); }
  refresh(source_id, key, profile_fingerprint) { return this.request('/v1/admin/refresh', {method: 'POST', body: {source_id, ...(profile_fingerprint ? {profile_fingerprint} : {})}, key}); }
  jobs(offset = 0, limit = 10) { return this.request(`/v1/jobs?${new URLSearchParams({offset, limit})}`); }
  job(id) { return this.request(`/v1/jobs/${encodeURIComponent(id)}`); }
  result(id) { return this.request(`/v1/jobs/${encodeURIComponent(id)}/result`); }
  report(id) { return this.request(`/v1/jobs/${encodeURIComponent(id)}/report`, {text: true}); }
  cancel(id) { return this.request(`/v1/jobs/${encodeURIComponent(id)}`, {method: 'DELETE'}); }
  logout() { return this.request('/v1/session', {method: 'DELETE'}); }
  offers(parameters) { return this.request(`/v1/offers?${new URLSearchParams(parameters)}`); }
}

export function recipePayload(form, capabilities, pantryRows) {
  const pantry = {};
  for (const {ingredient, quantity} of pantryRows) {
    if (!ingredient && !quantity) continue;
    if (!capabilities.ingredients.includes(ingredient)) throw new Error('Choose a known pantry ingredient.');
    if (Object.hasOwn(pantry, ingredient)) throw new Error('Each pantry ingredient may appear only once.');
    if (!/^(?:available|\d+(?:\.\d+)?\s*(?:g|kg))$/.test(quantity.trim())) throw new Error('Use positive grams/kg or “available” for pantry quantities.');
    if (quantity !== 'available' && /^0+(?:\.0+)?\s*(?:g|kg)$/.test(quantity)) throw new Error('Pantry quantities must be positive.');
    pantry[ingredient] = quantity.trim();
  }
  const payload = {provider: form.provider, cache_policy: form.cache_policy, source_ids: [capabilities.sources[0]], meal_style: form.meal_style, servings: Number(form.servings), max_stores: Number(form.max_stores), pantry, exclusions: form.exclusions};
  if (form.source_ids) {
    if (!form.source_ids.length || new Set(form.source_ids).size !== form.source_ids.length || form.source_ids.some(source => !capabilities.sources.includes(source))) throw new Error('Choose available grocery sources.');
    if (form.source_ids.length > 1 && !capabilities.combined_sources) throw new Error('Combined sources are not enabled on this server.');
    payload.source_ids = form.source_ids;
  }
  if (form.profile_fingerprints && Object.keys(form.profile_fingerprints).length) {
    for (const [source, fingerprint] of Object.entries(form.profile_fingerprints)) {
      if (!payload.source_ids.includes(source) || !/^(?:legacy|[0-9a-f]{24})$/.test(fingerprint)) throw new Error('Choose a published profile for each selected source.');
    }
    payload.profile_fingerprints = form.profile_fingerprints;
  }
  if (form.model) payload.model = form.model;
  for (const [field, value, positive] of [['min_protein_g', form.min_protein_g, false], ['max_kcal', form.max_kcal, true]]) {
    if (value === undefined || value === '') continue;
    if (!/^\d+(?:\.\d+)?$/.test(value) || (positive && /^0+(?:\.0+)?$/.test(value))) throw new Error('Nutrition limits must be valid decimal amounts.');
    payload[field] = value;
  }
  if (form.max_minutes) {
    const minutes = Number(form.max_minutes);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 480) throw new Error('Cooking time must be 1–480 minutes.');
    payload.max_minutes = minutes;
  }
  if (form.retailer_ids?.length) {
    if (new Set(form.retailer_ids).size !== form.retailer_ids.length || form.retailer_ids.some(id => !/^[a-z][a-z0-9_-]{0,63}$/.test(id))) throw new Error('Use unique valid retailer IDs.');
    payload.retailer_ids = form.retailer_ids;
  }
  if (typeof form.allow_loyalty === 'boolean') payload.allow_loyalty = form.allow_loyalty;
  if (form.use_first?.length) {
    if (form.use_first.some(id => !Object.hasOwn(pantry, id))) throw new Error('Use-first ingredients must be in your pantry.');
    payload.use_first = form.use_first;
  }
  if (form.seasonings_available === true) payload.seasonings_available = true;
  if (form.profile_fingerprint) {
    if (payload.source_ids.length !== 1 || payload.profile_fingerprints) throw new Error('Use per-source profiles for combined requests.');
    if (!/^(?:legacy|[0-9a-f]{24})$/.test(form.profile_fingerprint)) throw new Error('Choose a published grocery profile.');
    payload.profile_fingerprint = form.profile_fingerprint;
  }
  if (form.budget) {
    if (!/^\d+(?:\.\d{1,4})?$/.test(form.budget) || /^0+(?:\.0+)?$/.test(form.budget)) throw new Error('Budget must be a positive decimal amount.');
    payload.max_cost_per_serving_czk = form.budget; // Keep decimal precision over JSON.
  }
  if (!capabilities.providers.includes(payload.provider) || !capabilities.cache_policies.includes(payload.cache_policy)) throw new Error('Choose an available provider and cache policy.');
  return payload;
}

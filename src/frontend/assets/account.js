import {pantryItems} from './client.js';
import {language, t, errorMessage, sourceKey} from './i18n.js';

export const fieldLabels = {
  request_id: 'Request ID', servings: 'Servings', meal_style: 'Meal style', pantry: 'Pantry stock', exclusions: 'Exclusions',
  max_cost_per_serving_czk: 'Budget', max_stores: 'Maximum stores', retailer_ids: 'Retailer IDs · comma-separated',
  allow_loyalty: 'Loyalty prices', min_protein_g: 'Minimum protein', max_kcal: 'Maximum calories', max_minutes: 'Cooking time',
  use_first: 'Use first', seasonings_available: 'Seasonings available', provider: 'Recipe provider', model: 'Model',
  cache_policy: 'Grocery cache policy', source_ids: 'Sources', profile_fingerprint: 'Profile', profile_fingerprints: 'Profile', language: 'Recipe language',
};
const controls = {servings: 'servings', meal_style: 'meal-style', max_cost_per_serving_czk: 'budget', max_stores: 'max-stores',
  min_protein_g: 'min-protein', max_kcal: 'max-kcal', max_minutes: 'max-minutes', allow_loyalty: 'recipe-loyalty',
  seasonings_available: 'have-seasonings', provider: 'provider', model: 'model', cache_policy: 'cache-policy', language: 'recipe-language', retailer_ids: 'recipe-retailers'};
const policyLabels = {currency: 'Currency', location_label: 'Location', timezone: 'Timezone', lactose_free: 'Lactose free', ranking: 'Ranking', max_age_hours: 'Maximum data age · hours', provider_timeout_seconds: 'Provider timeout · seconds', context_ingredients: 'Context ingredients'};
const warningLabels = {'refresh-degraded': 'Fresh data could not be loaded. Saved data is being used.', 'coverage-unknown': 'Coverage is unknown for this catalogue.', 'legacy-coverage': 'This catalogue uses legacy coverage.'};

export class AccountUI {
  constructor(context) {
    Object.assign(this, context); this.busy = new Set(); this.clear();
    const on = (id, type, action) => this.$(id).addEventListener(type, () => this.run(action));
    on('save-pantry', 'click', () => this.saveStock()); on('reload-pantry', 'click', () => this.loadStock());
    this.$('settings-form').addEventListener('submit', event => { event.preventDefault(); this.run(() => this.savePreferences()); });
    on('reload-settings', 'click', () => this.loadPreferences());
    on('apply-preset', 'click', () => this.applyPreset());
    on('reload-presets', 'click', () => this.loadPresets());
    on('create-preset', 'click', () => this.writePreset('create'));
    on('update-preset', 'click', () => this.writePreset('update')); on('delete-preset', 'click', () => this.writePreset('delete'));
    this.$('preset-select').addEventListener('change', () => {
      const selected = this.selectedPreset(); this.$('preset-name').value = selected?.name ?? '';
      if (selected?.stale) this.status('preset', 'This preset is no longer supported. Update or delete it before applying.');
    });
    this.$('binding-form').addEventListener('submit', event => { event.preventDefault(); this.run(() => this.connect()); });
  }
  clear() {
    this.settings = this.stock = undefined; this.presets = []; this.connections = []; this.presetConflict = false; this.pendingLanguage = undefined; this.busy?.clear();
    for (const id of ['pantry-status', 'settings-status', 'preset-status', 'binding-status', 'binding-list', 'policy-details']) this.$(id)?.replaceChildren();
    for (const id of ['preset-name', 'binding-address', 'account-ui-language', 'account-recipe-language']) if (this.$(id)) this.$(id).value = '';
    for (const id of ['save-settings', 'save-pantry']) if (this.$(id)) this.$(id).disabled = true;
    this.$('preset-select')?.replaceChildren(this.option('', 'Choose a preset'));
  }
  option(value, label, literal = false) { const option = this.node('option'); option.textContent = literal ? label : t(label); option.value = value; return option; }
  status(id, key) { key = sourceKey(key); const target = this.$(id + '-status'); target.textContent = t(key); target.dataset.i18n = key; }
  async operation(name, action) {
    if (this.busy.has(name)) return;
    const active = this.generation(); this.busy.add(name);
    try { await action(active); }
    catch (error) {
      if (active !== this.generation()) return;
      if (error.status === 401) throw error;
      this.validation?.(error);
      this.status(name, error.status === 404 && ['settings', 'pantry'].includes(name) ? 'This feature is unavailable on this server.' : errorMessage(error));
      if (error.status === 409) {
        if (name === 'settings') { this.settings = undefined; this.$('save-settings').disabled = true; }
        if (name === 'pantry') { this.stock = undefined; this.$('save-pantry').disabled = true; }
        if (name === 'preset') { this.presetConflict = true; for (const id of ['update-preset', 'delete-preset']) this.$(id).disabled = true; }
      }
    } finally { if (active === this.generation()) this.busy.delete(name); }
  }
  async load() {
    await Promise.all([this.loadPreferences(), this.loadStock(), this.loadPresets(), this.loadBindings()]);
  }
  async loadPreferences() {
    return this.operation('settings', async active => {
      const value = await this.api.settings(); if (active !== this.generation()) return;
      if (!Number.isInteger(value.revision)) throw new Error('This feature is unavailable on this server.');
      this.settings = value;
      this.$('account-ui-language').value = value.ui_language ?? ''; this.$('account-recipe-language').value = value.recipe_language ?? '';
      this.$('recipe-language').value = value.recipe_language ?? this.capabilities().defaults?.language ?? 'en';
      language.account(value.ui_language); this.$('save-settings').disabled = false;
      this.status('settings', 'Loaded.'); this.relabel();
    });
  }
  async savePreferences(uiLanguage) {
    if (!this.settings) return;
    return this.operation('settings', async active => {
      this.status('settings', 'Saving…');
      const body = {expected_revision: this.settings.revision, ui_language: uiLanguage ?? (this.$('account-ui-language').value || null)};
      if (uiLanguage === undefined) body.recipe_language = this.$('account-recipe-language').value || null;
      const value = await this.api.saveSettings(body); if (active !== this.generation()) return;
      this.settings = value; this.$('account-ui-language').value = value.ui_language ?? '';
      if (uiLanguage === undefined) {
        if (value.ui_language) language.choose(value.ui_language); else language.resetChoice();
        language.account(value.ui_language); this.$('recipe-language').value = value.recipe_language ?? this.capabilities().defaults?.language ?? 'en';
      }
      this.status('settings', 'Saved.'); this.relabel();
    });
  }
  syncLanguage(value) {
    this.pendingLanguage = value;
    this.run(async () => {
      while (this.pendingLanguage && this.settings && !this.busy.has('settings')) {
        const next = this.pendingLanguage; this.pendingLanguage = undefined; await this.savePreferences(next);
      }
    });
  }
  async loadStock() {
    return this.operation('pantry', async active => {
      const value = await this.api.pantry(); if (active !== this.generation()) return;
      if (!Number.isInteger(value.revision) || !value.items) throw new Error('This feature is unavailable on this server.');
      this.stock = value; this.$('pantry-rows').replaceChildren();
      for (const [id, item] of Object.entries(value.items)) {
        const row = this.addPantry(); row.querySelector('select').value = id;
        row.querySelector('input').value = item.grams === null ? 'available' : `${item.grams}g`;
      }
      if (!Object.keys(value.items).length) this.addPantry();
      for (const option of this.$('use-first').options ?? []) option.selected = value.items[option.value]?.use_first === true;
      this.$('use-first').dispatchEvent?.(new Event('change'));
      this.$('save-pantry').disabled = false; this.status('pantry', 'Loaded.');
    });
  }
  async saveStock() {
    if (!this.stock) return;
    return this.operation('pantry', async active => {
      const rows = [...this.$('pantry-rows').children].map(row => ({ingredient: row.querySelector('select').value, quantity: row.querySelector('input').value}));
      for (const row of rows) if (row.ingredient && !this.capabilities().ingredients.includes(row.ingredient)) throw new Error('Choose a known pantry ingredient.');
      const useFirst = [...this.$('use-first').selectedOptions].map(option => option.value);
      if (useFirst.some(id => !rows.some(row => row.ingredient === id))) throw new Error('Use-first ingredients must be in your pantry.');
      this.status('pantry', 'Saving…');
      const value = await this.api.savePantry(pantryItems(rows, useFirst), this.stock.revision);
      if (active !== this.generation()) return; this.stock = value; this.status('pantry', 'Saved.');
    });
  }
  async loadPresets() {
    return this.operation('preset', async active => {
      const value = await this.api.presets(); if (active !== this.generation()) return;
      this.presets = value.items ?? []; this.presetConflict = false; for (const id of ['update-preset', 'delete-preset']) this.$(id).disabled = false; this.renderPresets();
      if (!this.presets.length) this.status('preset', 'No saved presets.');
    });
  }
  selectedPreset() { return this.presets.find(item => String(item.id ?? item.preset_id) === this.$('preset-select').value); }
  renderPresets() {
    const selected = this.$('preset-select').value;
    this.$('preset-select').replaceChildren(this.option('', 'Choose a preset'), ...this.presets.map(item => this.option(item.id ?? item.preset_id, item.name, true)));
    this.$('preset-select').value = selected;
  }
  async writePreset(mode) {
    if (this.presetConflict && mode !== 'create') return;
    return this.operation('preset', async active => {
      const selected = this.selectedPreset(), name = this.$('preset-name').value.trim();
      if (mode !== 'create' && !selected) throw new Error('Choose a preset first.');
      if (mode !== 'delete' && !name) throw new Error('Enter a preset name.');
      const id = selected?.id ?? selected?.preset_id;
      this.status('preset', 'Saving…');
      if (mode === 'delete') await this.api.deletePreset(id, selected.revision);
      else if (mode === 'update') await this.api.updatePreset(id, name, this.recipe(false), selected.revision);
      else await this.api.createPreset(name, this.recipe(false));
      if (active !== this.generation()) return;
      const value = await this.api.presets(); if (active !== this.generation()) return;
      this.presets = value.items; this.renderPresets(); this.status('preset', mode === 'delete' ? 'Preset deleted.' : 'Saved.');
    });
  }
  applyPreset() {
    const preset = this.selectedPreset(); if (!preset) throw new Error('Choose a preset first.');
    if (preset.stale) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    this.applyParameters(preset.parameters);
  }
  applyParameters(parameters) {
    const caps = this.capabilities();
    for (const [field, allowed] of [['provider', caps.providers], ['cache_policy', caps.cache_policies], ['meal_style', caps.meal_styles], ['language', caps.languages ?? ['cs', 'en']]]) {
      if (parameters[field] != null && !allowed.includes(parameters[field])) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    }
    for (const [field, allowed] of [['source_ids', caps.sources], ['exclusions', caps.ingredients], ['retailer_ids', caps.retailer_ids]]) {
      if (allowed?.length && parameters[field]?.some(value => !allowed.includes(value))) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    }
    for (const field of Object.keys(parameters)) if (!Object.hasOwn(fieldLabels, field) || (caps.recipe_request_schema?.properties && !Object.hasOwn(caps.recipe_request_schema.properties, field))) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    if (parameters.model && !(caps.models?.[parameters.provider] ?? []).includes(parameters.model)) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    for (const [source, fingerprint] of Object.entries(parameters.profile_fingerprints ?? (parameters.profile_fingerprint ? {[parameters.source_ids?.[0] ?? caps.sources[0]]: parameters.profile_fingerprint} : {}))) {
      const available = this.profileOptions ? this.profileOptions(source).some(([value]) => value === fingerprint) : caps.profile_metadata?.[source]?.some(item => item.fingerprint === fingerprint);
      if (!available) throw new Error('This preset is no longer supported. Update or delete it before applying.');
    }
    for (const [field, id] of Object.entries(controls)) {
      const value = parameters[field]; this.$(id).value = value == null ? (['servings', 'max_stores', 'meal_style', 'provider', 'cache_policy'].includes(field) ? this.capabilities().defaults?.[field] ?? '' : '') : Array.isArray(value) ? value.join(', ') : String(value);
      this.$(id).dispatchEvent?.(new Event('input'));
    }
    this.$('provider').dispatchEvent?.(new Event('change'));
    if (parameters.model != null) this.$('model').value = parameters.model;
    for (const [id, field] of [['exclusions', 'exclusions'], ['recipe-sources', 'source_ids']]) {
      for (const option of this.$(id).options ?? []) option.selected = (parameters[field] ?? this.capabilities().defaults?.[field] ?? []).includes(option.value);
      this.$(id).dispatchEvent?.(new Event('change'));
    }
    this.profiles();
    this.$('recipe-profile').value = parameters.profile_fingerprint ?? parameters.profile_fingerprints?.[parameters.source_ids?.[0]] ?? '';
    for (const select of this.$('extra-source-profiles').querySelectorAll('select')) select.value = parameters.profile_fingerprints?.[select.dataset.source] ?? '';
    this.$('recipe-language').value = parameters.language ?? this.settings?.recipe_language ?? this.capabilities().defaults?.language ?? 'en';
    this.status('preset', 'Preset applied.');
  }
  async loadBindings() {
    return this.operation('binding', async active => {
      const value = await this.api.bindings(); if (active !== this.generation()) return;
      this.connections = Array.isArray(value) ? value : value.items ?? []; this.renderBindings();
    });
  }
  async connect() {
    return this.operation('binding', async active => {
      await this.api.bind(this.$('binding-channel').value, this.$('binding-address').value.trim());
      if (active !== this.generation()) return; this.$('binding-address').value = '';
      const connections = await this.api.bindings(); if (active !== this.generation()) return; this.connections = connections;
      this.renderBindings(); this.status('binding', 'Connection challenge queued.');
    });
  }
  renderBindings() {
    this.$('binding-list').replaceChildren();
    for (const item of this.connections) {
      const row = this.node('li'); row.append(this.node('span', `${item.channel} · ${item.address} · ${t(item.status ?? (item.verified_at ? 'verified' : 'pending'))}`));
      const button = this.node('button', 'Disconnect', 'quiet'); button.type = 'button';
      button.addEventListener('click', () => this.run(() => this.operation('binding', async active => {
        await this.api.unbind(item.binding_id ?? item.id); if (active !== this.generation()) return;
        const connections = await this.api.bindings(); if (active !== this.generation()) return;
        this.connections = connections; this.renderBindings();
      })));
      row.append(button); this.$('binding-list').append(row);
    }
    if (!this.connections.length) this.status('binding', 'No connected channels.');
    const caps = this.capabilities();
    for (const option of this.$('binding-channel').options ?? []) option.disabled = !caps?.[`${option.value}_enabled`];
    this.$('add-binding').disabled = !(caps?.email_enabled || caps?.simplex_enabled);
  }
  render() {
    this.renderPresets(); this.renderBindings(); this.$('policy-details').replaceChildren();
    for (const [field, value] of Object.entries(this.capabilities()?.read_only_policy ?? {})) {
      this.$('policy-details').append(this.node('dt', policyLabels[field] ?? field), this.node('dd', typeof value === 'boolean' ? (value ? 'Yes' : 'No') : typeof value === 'object' ? JSON.stringify(value) : String(value)));
    }
  }
  warnings(target, state) {
    target.parentElement?.querySelector('.technical-warnings')?.remove();
    const codes = state.warning_codes ?? [], warnings = state.warnings ?? [];
    for (const code of codes) target.append(this.node('span', warningLabels[code] ?? 'Saved catalogue data may be incomplete.', 'warning'));
    if (warnings.length) {
      const details = this.node('details', undefined, 'technical-warnings'); details.append(this.node('summary', 'Technical details'));
      for (const warning of warnings) details.append(this.node('p', warning, undefined, true)); target.after(details);
      if (!codes.length) target.append(this.node('span', 'Saved catalogue data may be incomplete.', 'warning'));
    }
  }
}

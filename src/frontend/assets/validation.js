export const fieldControls = {servings: 'servings', meal_style: 'meal-style', max_cost_per_serving_czk: 'budget', max_stores: 'max-stores', min_protein_g: 'min-protein', max_kcal: 'max-kcal', max_minutes: 'max-minutes', retailer_ids: 'recipe-retailers', allow_loyalty: 'recipe-loyalty', seasonings_available: 'have-seasonings', provider: 'provider', model: 'model', cache_policy: 'cache-policy', language: 'recipe-language', source_ids: 'recipe-sources', profile_fingerprint: 'recipe-profile', profile_fingerprints: 'recipe-profile', exclusions: 'exclusions', use_first: 'use-first', name: 'preset-name', ui_language: 'account-ui-language', recipe_language: 'account-recipe-language', address: 'binding-address', channel: 'binding-channel'};
export function clearValidation(document, control) {
  for (const target of control ? [control] : document.querySelectorAll('[data-validation-error]')) {
    if (!target.dataset.validationError) continue;
    const id = target.dataset.validationError;
    document.getElementById(id)?.remove(); target.removeAttribute('aria-invalid'); delete target.dataset.validationError;
    const descriptions = (target.getAttribute('aria-describedby') ?? '').split(' ').filter(value => value && value !== id);
    if (descriptions.length) target.setAttribute('aria-describedby', descriptions.join(' ')); else target.removeAttribute('aria-describedby');
  }
}
export function showValidation(error, document, t, reveal) {
  if (!Array.isArray(error.fields)) return;
  let first;
  for (const field of error.fields) {
    const location = field.loc ?? [], key = [...location].reverse().find(value => Object.hasOwn(fieldControls, value));
    let target = key ? document.getElementById(fieldControls[key]) : undefined;
    if (location.includes('pantry') || location.includes('items')) {
      const id = location[location.indexOf(location.includes('pantry') ? 'pantry' : 'items') + 1];
      const rows = [...(document.getElementById('pantry-rows')?.children ?? [])];
      const row = rows.find(row => row.querySelector('select')?.value === id) ?? rows[0]; target = row?.querySelector('input');
    }
    if (!target) continue;
    if (target.getAttribute('aria-hidden') === 'true') target = document.getElementById(`${target.id}-search`) ?? document.getElementById(`${target.id}-choices-search`) ?? target;
    if (target.dataset.validationError) continue;
    if (!target.id) target.id = `pantry-quantity-${crypto.randomUUID()}`;
    const inline = document.createElement('span'); inline.id = `field-error-${target.id}`; inline.className = 'form-error'; inline.setAttribute('role', 'status');
    const message = key === 'min_protein_g' ? 'Protein must be between 0 and 300 g per serving.' : 'Check this value.';
    inline.dataset.i18n = message; inline.textContent = t(message); target.after(inline);
    target.dataset.validationError = inline.id; target.setAttribute('aria-invalid', 'true');
    target.setAttribute('aria-describedby', [target.getAttribute('aria-describedby'), inline.id].filter(Boolean).join(' '));
    if (!first) first = target;
  }
  if (first) { reveal(first); first.closest('details')?.setAttribute('open', ''); first.focus(); }
}

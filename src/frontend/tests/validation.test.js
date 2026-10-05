import test from 'node:test';
import assert from 'node:assert/strict';
import {showValidation, clearValidation} from '../assets/validation.js';

test('retailer validation targets visible search, deduplicates fields, and preserves descriptions', () => {
  const elements = new Map();
  const make = id => {
    const attributes = new Map();
    const element = {id, dataset: {}, getAttribute: key => attributes.get(key) ?? null,
      setAttribute: (key, value) => attributes.set(key, value), removeAttribute: key => attributes.delete(key),
      after(inline) { elements.set(inline.id, inline); }, closest: () => null,
      focus() { this.focused = true; }, remove() { elements.delete(this.id); }};
    elements.set(id, element); return element;
  };
  const canonical = make('recipe-retailers'); canonical.setAttribute('aria-hidden', 'true');
  const search = make('recipe-retailers-choices-search'); search.setAttribute('aria-describedby', 'existing-hint');
  const document = {getElementById: id => elements.get(id), createElement: () => make(''),
    querySelectorAll: () => [...elements.values()].filter(value => value.dataset.validationError)};
  let revealed;
  showValidation({fields: [{loc: ['body', 'retailer_ids', 0]}, {loc: ['body', 'retailer_ids', 1]}]}, document, key => key, value => { revealed = value; });
  assert.equal(revealed, search); assert.equal(search.focused, true);
  assert.equal(search.getAttribute('aria-invalid'), 'true');
  assert.equal(canonical.getAttribute('aria-invalid'), null);
  assert.equal(search.getAttribute('aria-describedby'), 'existing-hint field-error-recipe-retailers-choices-search');
  assert.equal([...elements.keys()].filter(id => id.startsWith('field-error')).length, 1);
  clearValidation(document, search);
  assert.equal(search.getAttribute('aria-invalid'), null);
  assert.equal(search.getAttribute('aria-describedby'), 'existing-hint');
  assert.equal(elements.has('field-error-recipe-retailers-choices-search'), false);
});

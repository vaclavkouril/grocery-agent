// Native selects remain the canonical control for existing callers and browser tests.
export function choiceList(select, t) {
  const originalTabIndex = select.getAttribute('tabindex'), originalAria = select.getAttribute('aria-hidden');
  select.setAttribute('tabindex', '-1'); select.setAttribute('aria-hidden', 'true');
  const wrapper = document.createElement('div'); wrapper.className = 'choice-list';
  const label = document.createElement('label'), search = document.createElement('input'); search.type = 'search';
  search.id = `${select.id}-search`; label.htmlFor = search.id;
  const count = document.createElement('p'); count.className = 'hint'; count.setAttribute('aria-live', 'polite');
  const list = document.createElement('div'); list.className = 'choice-options';
  wrapper.append(label, search, count, list); select.after(wrapper); select.classList.add('native-choices');
  const render = () => {
    const focused = list.contains(document.activeElement) ? document.activeElement?.dataset.optionValue : undefined;
    label.textContent = t('Search choices'); search.setAttribute('aria-label', `${t('Search choices')} · ${select.labels?.[0]?.textContent ?? select.id}`);
    count.textContent = t('Selected: {count}', {count: select.selectedOptions.length}); list.replaceChildren();
    const filter = search.value.toLocaleLowerCase();
    for (const option of select.options) {
      if (!(option.textContent + ' ' + option.value).toLocaleLowerCase().includes(filter)) continue;
      const label = document.createElement('label'); label.className = 'choice-chip';
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = option.selected;
      checkbox.dataset.optionValue = option.value;
      checkbox.disabled = select.disabled || option.disabled;
      checkbox.addEventListener('change', () => {
        if (!select.multiple) for (const other of select.options) other.selected = false;
        option.selected = checkbox.checked; select.dispatchEvent(new Event('change', {bubbles: true}));
      });
      label.append(checkbox, document.createTextNode(option.textContent)); list.append(label);
    }
    if (!list.childNodes.length) { const empty = document.createElement('p'); empty.textContent = t('No matching choices'); list.append(empty); }
    if (focused !== undefined) [...list.querySelectorAll('input')].find(input => input.dataset.optionValue === focused)?.focus({preventScroll: true});
  };
  search.addEventListener('input', render); select.addEventListener('change', render); render();
  return {render, destroy() {
    select.removeEventListener('change', render); select.classList.remove('native-choices'); wrapper.remove();
    for (const [key, value] of [['tabindex', originalTabIndex], ['aria-hidden', originalAria]]) if (value === null) select.removeAttribute(key); else select.setAttribute(key, value);
  }};
}

export function retailerChoices(input, ids, t) {
  const originalAria = input.getAttribute('aria-hidden'), originalTabIndex = input.getAttribute('tabindex');
  input.classList.add('native-choices'); input.setAttribute('aria-hidden', 'true'); input.setAttribute('tabindex', '-1');
  const select = document.createElement('select'); select.id = `${input.id}-choices`; select.multiple = true;
  for (const id of ids) { const option = document.createElement('option'); option.value = option.textContent = id; select.append(option); }
  input.after(select); const choices = choiceList(select, t);
  const sync = () => { const selected = input.value.split(',').map(value => value.trim()); for (const option of select.options) option.selected = selected.includes(option.value); select.disabled = input.disabled; choices.render(); };
  const update = () => { input.value = [...select.selectedOptions].map(option => option.value).join(', '); };
  input.addEventListener('input', sync); select.addEventListener('change', update); sync();
  return {render: sync, destroy() {
    input.removeEventListener('input', sync); select.removeEventListener('change', update); choices.destroy(); select.remove(); input.classList.remove('native-choices');
    for (const [key, value] of [['aria-hidden', originalAria], ['tabindex', originalTabIndex]]) if (value === null) input.removeAttribute(key); else input.setAttribute(key, value);
  }};
}

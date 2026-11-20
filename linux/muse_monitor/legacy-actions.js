async page => {
  const req = __MUSE_REQUEST__;
  const symbol = Object.getOwnPropertySymbols(page).find(s => s.description === 'tabSymbol');
  const selected = symbol && page[symbol];
  if (!selected) throw new Error('MUSE_PINNED_TAB_ADAPTER_CHANGED');
  const context = selected.context;
  const temporary = req.kind === 'modal-tabs' ? selected : null;
  if (temporary && page.url() !== 'about:blank') throw new Error('MUSE_NOT_OWN_INVENTORY_TAB');
  try {
  if (temporary) {
    const target = context.tabs()[req.restoreIndex];
    if (!target || target === temporary) throw new Error('MUSE_MODAL_TAB_INDEX_CHANGED');
    await context.selectTab(req.restoreIndex);
  }
  if (req.kind === 'select') {
    if (!Number.isInteger(req.index) || req.index < 0 || !context.tabs()[req.index])
      throw new Error('MUSE_TAB_INDEX_CHANGED_REFRESH_LIST');
    await context.selectTab(req.index);
  } else if (req.kind === 'read-value') {
    if (!/^(?:f\d+)?e\d+$/.test(req.ref)) throw new Error('MUSE_FRESH_REF_REQUIRED');
    const field = page.locator('aria-ref=' + req.ref);
    const kind = await field.evaluate(e => ({tag: e.tagName, type: e.type, autocomplete: e.autocomplete}));
    if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(kind.tag) ||
        ['password', 'hidden', 'file'].includes(kind.type) ||
        /(?:^|\s)(?:current-password|new-password|one-time-code)(?:$|\s)/.test(kind.autocomplete || ''))
      throw new Error('MUSE_FIELD_VALUE_NOT_AVAILABLE');
    const value = await field.inputValue({timeout: 2000});
    if (value.length > 16000) throw new Error('MUSE_FIELD_VALUE_EXCEEDS_BOUND');
    return {ref: req.ref, value, source: 'DOM inputValue', readOnly: true};
  } else if (req.kind === 'focused-input') {
    if (!/^(?:f\d+)?e\d+$/.test(req.ref)) throw new Error('MUSE_FRESH_REF_REQUIRED');
    await page.locator('aria-ref=' + req.ref).focus({timeout: 10000});
    if (req.operation === 'type') await page.keyboard.type(req.text);
    else if (req.operation === 'press') await page.keyboard.press(req.text);
    else throw new Error('MUSE_INPUT_OPERATION_DENIED');
    return {focusedRef: req.ref, operation: req.operation, dispatched: true};
  } else if (!['tabs', 'modal-tabs'].includes(req.kind)) throw new Error('MUSE_INTERNAL_OPERATION_DENIED');
  const tabs = context.tabs().slice();
  const entries = (await Promise.all(tabs.map(async (tab, index) => {
    if (tab === temporary) return null;
    // One pending title request per actual page, including a frozen renderer.
    const p = tab.page;
    p.__museLegacyTitle ??= {value: ''};
    const state = p.__museLegacyTitle;
    state.pending ??= p.title().then(t => state.value = t).catch(() => state.value).finally(() => state.pending = null);
    let timer;
    const title = await Promise.race([state.pending, new Promise(r => timer = setTimeout(() => r(state.value), 500))]).finally(() => clearTimeout(timer));
    return {index, current: context.currentTab() === tab, title: String(title).slice(0, 300), url: p.url(), chooserOpen: tab.modalStates().some(s => s.type === 'fileChooser')};
  }))).filter(Boolean);
  return {schema: 1, scope: 'all-open-tabs-in-assigned-browser', selectedIndex: entries.find(t => t.current)?.index ?? null, tabs: entries};
  } finally { if (temporary) await page.close(); }
}

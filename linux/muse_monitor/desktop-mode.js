async page => {
  const req = __MUSE_REQUEST__;
  const symbol = Object.getOwnPropertySymbols(page).find(s => s.description === 'tabSymbol');
  const selected = symbol && page[symbol];
  if (!selected) throw new Error('MUSE_PINNED_HANDOFF_ADAPTER_CHANGED');
  const native = selected.context;
  const context = page.context();
  const temporary = req.temporary ? selected : null;
  if (temporary && page.url() !== 'about:blank') throw new Error('MUSE_NOT_OWN_HANDOFF_TAB');
  context.__museDesktopMode ??= {human: false, pages: new WeakMap(), onPage: null};
  const state = context.__museDesktopMode;
  const counts = {removed: 0, restored: 0, cleared: 0, focusWarnings: 0};
  const focus = async (p, enabled) => {
    let cdp;
    try { cdp = await context.newCDPSession(p); await cdp.send('Emulation.setFocusEmulationEnabled', {enabled}); }
    catch { counts.focusWarnings++; }
    finally { if (cdp) await cdp.detach().catch(() => {}); }
  };
  const apply = async p => {
    if (p.isClosed()) return;
    let entry = state.pages.get(p);
    if (state.human) {
      if (!entry) {
        entry = {listeners: p.__museHumanFileChooserListeners || [], onNavigate: null};
        state.pages.set(p, entry);
      }
      for (const fn of p.listeners('filechooser')) {
        if (!entry.listeners.includes(fn)) entry.listeners.push(fn);
        p.removeListener('filechooser', fn); counts.removed++;
      }
      // Discard intercepted request objects, never alter the input's files.
      const tab = p[symbol];
      if (tab) for (const modal of tab.modalStates().slice()) {
        if (modal.type === 'fileChooser') { tab.clearModalState(modal); counts.cleared++; }
      }
      if (!entry.onNavigate) {
        entry.onNavigate = frame => {
          if (state.human && frame === p.mainFrame()) {
            entry.pendingFocus = (entry.pendingFocus || Promise.resolve()).then(() => state.human ? focus(p, false) : undefined);
          }
        };
        p.on('framenavigated', entry.onNavigate);
      }
      entry.pendingFocus = focus(p, false); await entry.pendingFocus;
    } else if (entry || p.__museHumanFileChooserListeners) {
      const listeners = entry?.listeners || p.__museHumanFileChooserListeners;
      for (const fn of listeners) if (!p.listeners('filechooser').includes(fn)) {
        p.on('filechooser', fn); counts.restored++;
      }
      if (entry?.onNavigate) p.removeListener('framenavigated', entry.onNavigate);
      if (entry?.pendingFocus) await entry.pendingFocus.catch(() => {});
      state.pages.delete(p); delete p.__museHumanFileChooserListeners;
      await focus(p, true);
    }
  };
  try {
    state.human = !!req.human;
    if (state.human && !state.onPage) {
      state.onPage = p => apply(p).catch(() => {});
      context.on('page', state.onPage);
    } else if (!state.human && state.onPage) {
      context.removeListener('page', state.onPage); state.onPage = null;
    }
    for (const tab of native.tabs().slice()) await apply(tab.page);
    return {mode: state.human ? 'human' : 'agent', ...counts, filesChanged: false};
  } finally {
    if (temporary) {
      try { await native.selectTab(req.restoreIndex); }
      finally { await page.close(); }
    }
  }
}

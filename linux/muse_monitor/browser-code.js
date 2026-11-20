async page => {
  const req = __MUSE_REQUEST__;
  const operationStarted = Date.now();
  let navigationMs;
  const perform = async () => {
  const context = page.context();
  const nonce = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
  context.__museMonitor ??= { generation: nonce(), next: 0 };
  const identify = p => {
    if (!p.__museMonitor) {
      p.__museMonitor = { id: 'p' + (++context.__museMonitor.next), document: nonce(), owner: null };
      p.on('framenavigated', frame => { if (frame === p.mainFrame()) p.__museMonitor.document = nonce(); });
    }
    return p.__museMonitor;
  };
  for (const p of context.pages()) identify(p);
  const describe = async p => {
    const state = identify(p);
    // Keep at most one pending title read per page, including hung background
    // pages. Repeated sampling must not accumulate uncancelled RPC promises.
    state.pendingTitle ??= p.title().then(t => state.title = t).catch(() => state.title || '').finally(() => { state.pendingTitle = null; });
    let timer;
    const title = await Promise.race([state.pendingTitle, new Promise(r => { timer = setTimeout(() => r(state.title || '(title unavailable)'), 700); })]).finally(() => clearTimeout(timer));
    return { id: identify(p).id, title: String(title).slice(0, 300), generation: identify(p).document, url: p.url(), managed: !!identify(p).owner };
  };
  if (req.kind === 'describe') return { browser: context.__museMonitor.generation, pages: await Promise.all(context.pages().map(describe)) };
  if (req.kind === 'new') {
    const p = await context.newPage(); identify(p).owner = req.owner;
    const navigationStarted = Date.now();
    await p.goto(req.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    navigationMs = Date.now() - navigationStarted;
    return await describe(p);
  }
  const p = context.pages().find(p => identify(p).id === req.page);
  if (!p || identify(p).document !== req.document) throw new Error('MUSE_STALE_PAGE');
  const ref = () => p.locator('aria-ref=' + req.ref);
  switch (req.kind) {
    case 'receipt': {
      // Read only: no navigation, focus changes, form input or broad snapshot
      // persistence. Require the caller's exact current URL and document.
      if (p.url() !== req.url) throw new Error('MUSE_STALE_PAGE');
      const visible = await p.locator('body').innerText({ timeout: 3000 });
      if (identify(p).document !== req.document || p.url() !== req.url) throw new Error('MUSE_STALE_PAGE');
      return { page: await describe(p), matched: visible.includes(req.text) };
    }
    case 'adopt': identify(p).owner = 'human-adopted'; return { page: await describe(p) };
    case 'snapshot': return { page: await describe(p), snapshot: await p.ariaSnapshot({ mode: 'ai', timeout: 10000 }) };
    case 'frame': await p.screenshot({ path: req.path, type: 'png', animations: 'disabled', caret: 'hide', timeout: 4000, scale: 'css' }); return { page: await describe(p) };
    case 'goto': {
      const navigationStarted = Date.now();
      await p.goto(req.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
      navigationMs = Date.now() - navigationStarted; break;
    }
    case 'performance': return { page: await describe(p), lastDocumentNavigation: await p.evaluate(() => {
      const n = performance.getEntriesByType('navigation')[0];
      return n ? {responseWaitMs:Math.max(0,n.responseStart-n.requestStart),downloadMs:Math.max(0,n.responseEnd-n.responseStart),domReadyMs:n.domContentLoadedEventEnd,loadMs:n.loadEventEnd} : null;
    }) };
    case 'click': await ref().click({ timeout: 10000 }); break;
    case 'fill': {
      await ref().fill(req.text, { timeout: 10000 });const observed=await ref().inputValue({ timeout: 2000 });
      return { page: await describe(p), ...(req.sensitive ? { observedMatches: observed === req.text } : { observed }) };
    }
    case 'check': await ref().setChecked(!!req.checked, { timeout: 10000 }); break;
    case 'select': await ref().selectOption(req.value, { timeout: 10000 }); break;
    case 'upload': await ref().setInputFiles(req.path, { timeout: 10000 }); return { page: await describe(p), files: await ref().evaluate(e => Array.from(e.files || [], f => ({ name: f.name, size: f.size }))) };
    case 'text': await p.keyboard.insertText(req.text); break;
    case 'key': await p.keyboard.press(req.key); break;
    case 'point': await p.mouse.click(req.x * req.width, req.y * req.height); break;
    case 'close':
      if (identify(p).owner !== req.owner || context.pages().length < 2) throw new Error('MUSE_PROTECTED_TAB');
      await p.close(); return { closed: true };
    default: throw new Error('MUSE_UNSUPPORTED_ACTION');
  }
  return { page: await describe(p) };
  };
  const result = await perform();
  return {...result, __museTiming:{browserOperationMs:Date.now()-operationStarted,...(navigationMs===undefined?{}:{navigationMs})}};
}

// Adapted from CoS retireAutomaticTab / organizeManagedTabs guard ordering.
// Copyright (c) 2026 Chat On Steroids contributors. MIT; see THIRD-PARTY-NOTICES.
export function sameIncarnation(before, after, documentBefore, documentAfter) {
  return !!before && !!after && Number.isInteger(before.id) && before.id === after.id &&
    before.url === after.url && before.windowId === after.windowId &&
    !after.pinned && !after.pendingUrl && !(after.active && !before.active) &&
    !!documentBefore && documentBefore === documentAfter;
}
export function ruleFor(tab, rules) {
  let u; try { u = new URL(tab.url); } catch { return null; }
  if (!['https:', 'http:'].includes(u.protocol)) return null;
  return [...rules].sort((a,b) => b.priority-a.priority || a.id.localeCompare(b.id)).find(r =>
    r.domains.some(d => u.hostname === d || u.hostname.endsWith('.'+d)) &&
    (!r.path || u.pathname.startsWith(r.path))) || null;
}
export function validateRules(rules) {
  const colors = ['grey','blue','red','yellow','green','pink','purple','cyan','orange'];
  if (!Array.isArray(rules) || rules.length > 100) throw Error('Use at most 100 rules.');
  const ids = new Set();
  for (const r of rules) {
    if (!/^[a-z0-9_-]{1,64}$/.test(r.id) || ids.has(r.id) || typeof r.title !== 'string' || !r.title.trim() || r.title.length > 100 || !colors.includes(r.color) || !Number.isInteger(r.priority) || !Array.isArray(r.domains) || !r.domains.length || r.domains.some(d => !/^[a-z0-9.-]+$/.test(d) || d.startsWith('.') || d.endsWith('.')) || r.path && (typeof r.path !== 'string' || !r.path.startsWith('/'))) throw Error('Invalid grouping rule.');
    ids.add(r.id);
  }
  return rules;
}
export function safeCollectionURL(value) {
  try { const u=new URL(value); return ['http:','https:'].includes(u.protocol) && !u.username && !u.password && ![...u.searchParams.keys()].some(k => /^(access_token|refresh_token|password|otp|code|auth)$/i.test(k)); } catch {return false;}
}
export function duplicateSuggestions(tabs) {
  const groups = new Map();
  for (const t of tabs) { if(!safeCollectionURL(t.url)) continue; const list=groups.get(t.url)||[];list.push(t);groups.set(t.url,list); }
  return [...groups.values()].filter(g=>g.length>1).map(g=>({title:g[0].title,count:g.length,ids:g.map(t=>t.id),action:'suggest only; duplicates may have different unsaved work'}));
}

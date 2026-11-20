import {sameIncarnation,ruleFor,validateRules,safeCollectionURL,duplicateSuggestions} from './guards.mjs';
import {record} from './diagnostics.mjs';
const HOST='com.muse.monitor';
let flight=Promise.resolve();
const serial = fn => { const p=flight.catch(()=>{}).then(fn);flight=p;return p; };
async function native(body) {
  const start=performance.now();
  try {const r=await chrome.runtime.sendNativeMessage(HOST,body);if(r.error)throw Error(r.error);if(performance.now()-start>1000)void record('native-slow',performance.now()-start);return r;}
  catch(e){void record('native-error',performance.now()-start);throw e;}
}
self.addEventListener('error',()=>{void record('background-error');});
self.addEventListener('unhandledrejection',()=>{void record('background-rejection');});
async function sharedRules(value) {
  const old=await chrome.storage.local.get(['rules','rulesRevision']);
  if(value!==undefined) {
    validateRules(value);
    const result=await native({op:'rules',rules:value,revision:old.rulesRevision??0});
    await chrome.storage.local.set({rules:result.rules,rulesRevision:result.revision});return result.rules;
  }
  try {
    const result=await native({op:'rules'});
    await chrome.storage.local.set({rules:result.rules,rulesRevision:result.revision});return result.rules;
  } catch {return old.rules||[];}
}
async function inventory() {
  const tabs=await chrome.tabs.query({});
  const all=await chrome.storage.session.get(tabs.map(t=>'tab-'+t.id));
  return Promise.all(tabs.map(async t=> {
    const state=all;
    return {...t,dirty:state['tab-'+t.id]?.dirty??null,documentId:state['tab-'+t.id]?.documentId??null,manual:state['tab-'+t.id]?.manual??false};
  }));
}
chrome.runtime.onInstalled.addListener(async()=> {
  const old=await chrome.storage.local.get(['rules','collections']);
  if(!old.rules)await chrome.storage.local.set({rules:[]});
  if(!old.collections)await chrome.storage.local.set({collections:{}});
});
chrome.tabs.onRemoved.addListener(id=>chrome.storage.session.remove('tab-'+id));
chrome.tabs.onUpdated.addListener(async(id,change)=> {
  if(change.status==='loading' || change.url)await chrome.storage.session.remove('tab-'+id);
});
chrome.tabGroups.onUpdated.addListener(async group=> {
  // Human collapse/title changes persist; reconciliation never expands them.
  const state=await chrome.storage.local.get('groups');
  const groups=state.groups||{};groups[group.id]={title:group.title,collapsed:group.collapsed,color:group.color};
  await chrome.storage.local.set({groups});
});
async function groupNow() {
  return serial(async()=> {
    const rules=await sharedRules();validateRules(rules);
    const tabs=(await inventory()).filter(t=>!t.manual && !t.pinned && !t.pendingUrl && t.dirty===false && t.documentId && ruleFor(t,rules));
    if(!tabs.length)return {grouped:0,reason:'No proven-safe managed candidates. Pause and explicitly manage tabs first.'};
    const requestId=crypto.randomUUID();const permit=await native({op:'tab-permit',requestId,tabs:tabs.map(t=>({id:t.id,url:t.url,windowId:t.windowId,documentId:t.documentId,dirty:t.dirty,pinned:t.pinned,pending:!!t.pendingUrl}))});
    let success=false,count=0;
    try {
      // Persist before acting, then reread physical/document identity, as in CoS.
      await chrome.storage.session.set({pendingGroup:{requestId,started:Date.now()}});
      for(const proof of permit.tabs) {
        if(Date.now()/1000>permit.expiresAt)throw Error('Grouping authorization expired; reconcile first.');
        const before=tabs.find(t=>t.id===proof.id);const after=await chrome.tabs.get(proof.id);
        const s=(await chrome.storage.session.get('tab-'+proof.id))['tab-'+proof.id];
        if(!sameIncarnation(before,after,proof.documentId,s?.documentId) || s?.dirty!==false || s?.manual)continue;
        const rule=ruleFor(after,rules);if(!rule)continue;
        const groups=await chrome.tabGroups.query({windowId:after.windowId,title:rule.title});
        // Never adopt a lookalike group containing unknown/unpermitted tabs.
        let target;
        for(const g of groups) { const members=await chrome.tabs.query({groupId:g.id});if(members.every(t=>permit.tabs.some(p=>p.id===t.id))) {target=g;break;} }
        const id=await chrome.tabs.group({tabIds:[after.id],...(target?{groupId:target.id}:{})});
        await chrome.tabGroups.update(id,{title:rule.title,color:rule.color});count++;
      }
      success=true;return {grouped:count};
    } finally { await native({op:'tab-finish',requestId,success}).catch(()=>{});if(success)await chrome.storage.session.remove('pendingGroup'); }
  });
}
async function perform(m) {
  switch(m.op) {
    case 'diagnostics': return {schema:1,events:(await chrome.storage.local.get('diagnostics')).diagnostics||[],maxEvents:50,contentsLogged:false};
    case 'inventory': {let status;try {status=await native({op:'status'});}catch {status={offline:true};}const tabs=await inventory();return {tabs,status,duplicates:duplicateSuggestions(tabs)};}
    case 'jump': {const t=await chrome.tabs.get(m.id);await chrome.tabs.update(t.id,{active:true});await chrome.windows.update(t.windowId,{focused:true});return {opened:true};}
    case 'manual': {const key='tab-'+m.id;const s=(await chrome.storage.session.get(key))[key]||{};await chrome.storage.session.set({[key]:{...s,manual:!!m.value}});return {saved:true};}
    case 'rules': return sharedRules(m.value);
    case 'group': return groupNow();
    case 'capture': {
      const tab=await chrome.tabs.get(m.id);if(!safeCollectionURL(tab.url))throw Error('Login/challenge URLs cannot be captured.');
      const item={url:tab.url,title:tab.title,kind:m.kind||'unclassified',note:String(m.note||'').slice(0,2000)};
      try {return await native({op:'capture',item});} catch {
        const {captures=[]}=await chrome.storage.local.get('captures');captures.push({id:crypto.randomUUID(),item,pending:true});await chrome.storage.local.set({captures:captures.slice(-500)});return {state:'pending',reason:'Controller offline; capture is saved locally and not yet synced.'};
      }
    }
    case 'flush': {
      const {captures=[]}=await chrome.storage.local.get('captures');const pending=[];
      for(const c of captures) {try {await native({op:'capture',item:c.item});}catch {pending.push(c);}}
      await chrome.storage.local.set({captures:pending});return {pending:pending.length};
    }
    case 'collections': return (await chrome.storage.local.get('collections')).collections||{};
    case 'save-collection': {
      if(typeof m.name!=='string'||!m.name.trim()||m.name.length>100)throw Error('Name the collection.');
      const tabs=await inventory();const old=(await chrome.storage.local.get('collections')).collections||{};
      old[m.name]=tabs.filter(t=>safeCollectionURL(t.url)).map(t=>({title:t.title,url:t.url}));await chrome.storage.local.set({collections:old});return {saved:old[m.name].length};
    }
    case 'open-collection': {const data=(await chrome.storage.local.get('collections')).collections||{};const rows=data[m.name];if(!Array.isArray(rows)||rows.length>100)throw Error('Invalid collection.');for(const t of rows){if(safeCollectionURL(t.url))await chrome.tabs.create({url:t.url,active:false});}return {opened:rows.length};}
    case 'export': return {schema:1,...await chrome.storage.local.get(['rules','collections'])};
    case 'import': {
      const data=m.value;if(data?.schema!==1)throw Error('Unsupported collection format.');validateRules(data.rules||[]);
      if(typeof data.collections!=='object'||Object.keys(data.collections).length>100)throw Error('Invalid collections.');
      for(const rows of Object.values(data.collections)){if(!Array.isArray(rows)||rows.length>100||rows.some(t=>!safeCollectionURL(t.url)))throw Error('Invalid collection URL.');}
      await sharedRules(data.rules||[]);await chrome.storage.local.set({collections:data.collections});return {imported:true};
    }
    case 'pause': case 'takeover': case 'resume': case 'adopt': return native(m);
    default:throw Error('Unknown tool.');
  }
}
chrome.runtime.onMessage.addListener((m,sender,reply)=> {
  const ownUI=sender.id===chrome.runtime.id && sender.url?.startsWith(chrome.runtime.getURL(''));
  if(sender.tab && !ownUI) {
    if(m.op==='dirty' && sender.documentId && typeof m.dirty==='boolean') {
      const key='tab-'+sender.tab.id;chrome.storage.session.get(key).then(s=>chrome.storage.session.set({[key]:{manual:s[key]?.manual||false,documentId:sender.documentId,dirty:m.dirty}})).then(()=>reply({recorded:true}));return true;
    }
    return false; // Page/content data can never request control or reveal pairing.
  }
  if(!ownUI)return false;
  perform(m).then(reply,e=>{void record('action-error');reply({error:e.message});});return true;
});

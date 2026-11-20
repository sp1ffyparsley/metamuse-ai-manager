// Local metadata only; no page URLs, titles, inputs, messages or stacks.
const CODES=new Set(['native-error','native-slow','background-error','background-rejection','action-error']);
export function boundedEvent(code,ms,now=Date.now()) {
  if(!CODES.has(code))return null;
  return {at:now,code,...Number.isFinite(ms)?{ms:Math.max(0,Math.min(60000,Math.round(ms)))}:{}};
}
export function appendEvent(rows,event) {
  const safe=Array.isArray(rows)?rows.filter(r=>CODES.has(r?.code)&&Number.isFinite(r.at)).map(r=>boundedEvent(r.code,r.ms,r.at)):[];
  if(!event)return safe.slice(-50);
  const last=safe.findLast(r=>r.code===event.code);
  if(last && event.at-last.at<30000)return safe.slice(-50);
  return [...safe,event].slice(-50);
}
let flight=Promise.resolve();
export function record(code,ms) {
  flight=flight.catch(()=>{}).then(async()=> {
    const old=(await chrome.storage.local.get('diagnostics')).diagnostics;
    await chrome.storage.local.set({diagnostics:appendEvent(old,boundedEvent(code,ms))});
  }).catch(()=>{});
  return flight;
}

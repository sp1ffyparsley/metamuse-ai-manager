// Only a boolean is reported; no form fields, tokens, or keystrokes are captured.
let dirty=[...document.querySelectorAll('input,textarea,select')].some(e=>
  e.type==='checkbox'||e.type==='radio'?e.checked!==e.defaultChecked:
  e.tagName==='SELECT'?[...e.options].some(o=>o.selected!==o.defaultSelected):e.value!==e.defaultValue);
const report=()=>chrome.runtime.sendMessage({op:'dirty',dirty}).catch(()=>{});
addEventListener('input',()=>{dirty=true;report();},true);
addEventListener('change',()=>{dirty=true;report();},true);
addEventListener('pageshow',report);
report();

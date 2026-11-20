const $=id=>document.getElementById(id);let current=null;
async function call(body){const r=await chrome.runtime.sendMessage(body);if(r?.error)throw Error(r.error);return r;}
function notice(text){$('status').textContent=text;}
function button(text,fn){const b=document.createElement('button');b.textContent=text;b.onclick=()=>Promise.resolve().then(fn).catch(e=>notice(e.message));return b;}
async function render(refresh=true){
 if(refresh || !current) {
 current=await call({op:'inventory'});const result=current.status;const workspace=result.status?.workspaces?.[0];
 notice(workspace?`${workspace.name} · ${workspace.state} · ${workspace.actor||'no observed worker'}${workspace.blocker?' · '+workspace.blocker:''}`:'Controller offline. Ordinary tab tools remain available; managed work stays protected.');
 }
 const workspace=current.status.status?.workspaces?.[0];
 $('tabs').replaceChildren();const search=$('search').value.toLowerCase();
 for(const t of current.tabs.filter(t=>(t.title+' '+t.url).toLowerCase().includes(search))){const row=document.createElement('div');row.className='tab';const title=document.createElement('strong');title.textContent=t.title||'Untitled tab';row.append(title);const flags=document.createElement('p');flags.className='muted';flags.textContent=t.pinned?'Pinned · protected':t.dirty===true?'Unsaved edits · protected':t.dirty===null?'Not inspected · protected':'Clean observation; authority still required';row.append(flags);
 row.append(button('Open',()=>call({op:'jump',id:t.id})),button('Capture',async()=>{const r=await call({op:'capture',id:t.id,kind:$('kind').value,note:$('note').value});notice(r.state==='pending'?'Capture pending; controller has not received it.':'Captured into pending work. This does not start an agent.');}),button(t.manual?'Allow rule grouping':'Keep my arrangement',()=>call({op:'manual',id:t.id,value:!t.manual}).then(render)));
 if(workspace?.state==='paused'){const matches=workspace.pages.filter(p=>p.url===t.url);if(matches.length===1)row.append(button('Manage this tab',()=>call({op:'adopt',page:matches[0].id,pageGeneration:matches[0].generation}).then(render)));}
 $('tabs').append(row);}
 $('duplicates').textContent=current.duplicates.map(d=>`${d.count} tabs: ${d.title} — inspect before closing`).join('\n')||'No duplicate URL suggestions.';
 if(!refresh)return;
 const rules=await call({op:'rules'});$('rules').textContent=rules.map(r=>`${r.domains.join(', ')} → ${r.title}`).join('\n')||'No grouping rules yet.';
 const collections=await call({op:'collections'});$('collections').replaceChildren();for(const name of Object.keys(collections))$('collections').append(button(`Open ${name}`,()=>call({op:'open-collection',name})));
}
$('refresh').onclick=()=>render().catch(e=>notice(e.message));let searchTimer;$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>render(false).catch(e=>notice(e.message)),120);};
$('diagnostics').onclick=()=>call({op:'diagnostics'}).then(r=>{$('diagnostic-log').value=JSON.stringify(r,null,2);}).catch(e=>notice(e.message));
$('pause').onclick=async()=>{try{const w=current.status.status.workspaces[0];await call({op:'pause',requestId:crypto.randomUUID(),generation:w.generation});await render();}catch(e){notice(e.message);}};
$('group').onclick=()=>call({op:'group'}).then(r=>notice(`${r.grouped} tabs grouped. ${r.reason||''}`)).catch(e=>notice(e.message));
$('retry').onclick=()=>call({op:'flush'}).then(r=>notice(`${r.pending} captures still pending.`)).catch(e=>notice(e.message));
$('add-rule').onclick=async()=>{try{const rules=await call({op:'rules'});rules.push({id:crypto.randomUUID(),domains:[$('domain').value.trim().toLowerCase()],title:$('group-title').value.trim(),color:$('color').value,priority:0});await call({op:'rules',value:rules});await render();}catch(e){notice(e.message);}};
$('save-collection').onclick=()=>call({op:'save-collection',name:$('collection-name').value.trim()}).then(render).catch(e=>notice(e.message));
$('export').onclick=async()=>{try{$('import-data').value=JSON.stringify(await call({op:'export'}),null,2);notice('Copy the saved data below to export it. It contains links only.');}catch(e){notice(e.message);}};
$('import').onclick=()=>Promise.resolve().then(()=>call({op:'import',value:JSON.parse($('import-data').value)})).then(render).catch(e=>notice(e.message));
render().catch(e=>notice(e.message));

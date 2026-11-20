"""Opt-in diagnosis: one own public DDG tab; no search or form submission."""
import json,re,shlex,subprocess,time
from pathlib import Path
from muse_monitor.browser import Browser
from muse_monitor.daemon import execute

cfg=json.loads((Path.home()/'.config/muse-monitor/config.json').read_text())
browser=Browser(cfg);token=None;own=None;prior=None
report={'checks':{},'measurements':[]}
def call(*args):
    r=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=__import__('os').environ|{'SSH_ORIGINAL_COMMAND':shlex.join(args)},capture_output=True,text=True,timeout=50)
    if r.returncode:raise AssertionError('Handler failed: '+args[0])
    return r.stdout
def run(*args):
    start=time.monotonic();out=call('run',token,*args)
    report['measurements'].append({'command':args[0],'ms':round((time.monotonic()-start)*1000,2)})
    return out
def val(out):return json.JSONDecoder().raw_decode(out.split('### Result\n',1)[1].strip())[0]
def direct(code):
    spec,runtime,base,cwd=browser.paths('jobs')
    with browser.exclusive('jobs'):
        r=execute(base,spec['session'],cwd,('run-code',code),15)
    assert r and not r.returncode
    return val(r.stdout)
before=browser.describe('jobs',True)['pages'];before_ids={p['id'] for p in before}
try:
    token=call('claim').strip();assert re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{31}',token)
    prior=val(run('tab-list'))['selectedIndex']
    run('tab-new','https://duckduckgo.com/')
    added=[p for p in browser.describe('jobs',True)['pages'] if p['id'] not in before_ids]
    assert len(added)==1;own=added[0]['id']
    for _ in range(3):run('tab-list');run('snapshot')
    snap=run('snapshot');ref=re.search(r'combobox "Search with DuckDuckGo"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    for text in ('marketing intern','another phrase'):
        run('fill',ref,text);snap=run('snapshot')
        observed=direct('async page => { if (new URL(page.url()).hostname !== "duckduckgo.com") throw new Error("OWN_TEST_PAGE_CHANGED"); const e=page.getByRole("combobox",{name:"Search with DuckDuckGo",exact:true}); return {value:await e.inputValue(),textContent:await e.textContent(),tag:await e.evaluate(x=>x.tagName)}; }')
        readback=val(run('read-value',ref))
        line=next(line for line in snap.splitlines() if 'combobox "Search with DuckDuckGo"' in line)
        report['checks']['actual_value_exact_'+text.replace(' ','_')]=observed['value']==text
        report['checks']['handler_readback_exact_'+text.replace(' ','_')]=readback['value']==text and readback['readOnly'] is True
        report.setdefault('ddg',[]).append({'expected':text,'observed':observed,'snapshotLine':line})
        ref=re.search(r'\[ref=([A-Za-z0-9]+)\]',line).group(1)
    report['complete']=True
finally:
    if token:
        if own:
            pages=browser.describe('jobs',True)['pages']
            if any(p['id']==own for p in pages):
                run('tab-close',str(next(i for i,p in enumerate(pages) if p['id']==own)))
        if prior is not None:run('tab-select',str(prior))
        call('release',token)
    after=browser.describe('jobs',True)['pages']
    report['checks']['original_pages_preserved']=before_ids<={p['id'] for p in after}
    report['checks']['own_tab_removed']=not any(p['id']==own for p in after)
    report['complete']=report.get('complete',False) and all(report['checks'].values())
    print(json.dumps(report),flush=True)
    if not report['complete']:raise SystemExit(1)

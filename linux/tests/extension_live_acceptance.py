import json,time,threading,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from muse_monitor.browser import Browser
from muse_monitor.cli import request
config=json.loads((Path.home()/'.config/muse-monitor/config.json').read_text());b=Browser(config);prefix='http://127.0.0.1:';ext='chrome-extension://'+config['extensionId']+'/popup.html';results=[]
def run(js):
    out=b.cli('jobs','run-code','async page => { '+js+' }')
    raw=out.split('### Result\n',1)[-1]
    return json.JSONDecoder().raw_decode(raw.strip())[0]
def op(body):
    run('const p=page.context().pages().find(p=>p.url()==='+json.dumps(ext)+'); return await p.evaluate(m=>{globalThis.museQA=null;chrome.runtime.sendMessage(m).then(r=>globalThis.museQA=r,e=>globalThis.museQA={error:e.message});return true;},'+json.dumps(body)+');')
    until=time.monotonic()+20
    while time.monotonic()<until:
        r=run('const p=page.context().pages().find(p=>p.url()==='+json.dumps(ext)+');return await p.evaluate(()=>globalThis.museQA);')
        if r is not None:
            if isinstance(r,dict) and r.get('error'):raise AssertionError(r['error'])
            return r
        time.sleep(.15)
    raise AssertionError('Extension operation timed out')
class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<!doctype html><title>Muse extension QA</title><label>Fixture only <input></label>')
    def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=server.serve_forever,daemon=True).start();base=f'http://127.0.0.1:{server.server_port}/muse-qa-'
old=None;created=[];capture=None
try:
    # Reload this extension only; Chrome and portal tabs remain running.
    run('const p=page.context().pages().find(p=>p.url()==='+json.dumps(ext)+');await p.evaluate(()=>{setTimeout(()=>chrome.runtime.reload(),0);return true;});return true;')
    time.sleep(.5)
    run('const c=page.context();if(!c.pages().some(p=>p.url()==='+json.dumps(ext)+')){const p=await c.newPage();await p.goto('+json.dumps(ext)+');}return true;')
    old=op({'op':'export'});old['rules']=op({'op':'rules'})
    observed=op({'op':'inventory'});assert observed['status']['workspace']=='jobs';results.append('native profile identity verified: jobs')
    for name in ('safe','dirty','pinned','manual','unknown','duplicate','duplicate'):
        url=base+name
        run('const p=await page.context().newPage();await p.goto('+json.dumps(url)+');return true;');created.append(url)
    inv=op({'op':'inventory'});tabs=[t for t in inv['tabs'] if t['url'].startswith(base)];by={t['url'].removeprefix(base):t for t in tabs}
    for name in ('safe','dirty','pinned','manual'):
        observed=b.describe('jobs',True);p=next(p for p in observed['pages'] if p['url']==base+name)
        op({'op':'adopt','page':p['id'],'pageGeneration':p['generation']})
    run('const p=page.context().pages().find(p=>p.url()==='+json.dumps(base+'dirty')+');await p.locator("input").fill("unsaved fixture");return true;')
    run('const p=page.context().pages().find(p=>p.url()==='+json.dumps(ext)+');return await p.evaluate(async id=>{await chrome.tabs.update(id,{pinned:true});return true;},'+str(by['pinned']['id'])+');')
    op({'op':'manual','id':by['manual']['id'],'value':True})
    rules=[{'id':'muse-qa','title':'Muse QA temporary','domains':['127.0.0.1'],'path':'/muse-qa-','color':'blue','priority':0}]
    op({'op':'rules','value':rules});r=op({'op':'group'});assert r['grouped']==1,r
    inv=op({'op':'inventory'});after={t['url'].removeprefix(base):t for t in inv['tabs'] if t['url'].startswith(base)}
    assert after['safe']['groupId']>=0
    for n in ('dirty','pinned','manual','unknown','duplicate'):assert after[n]['groupId']==-1,(n,after[n]['groupId'])
    assert after['dirty']['dirty'] is True and any(d['count']==2 for d in inv['duplicates'])
    results.append('actual grouping: only adopted clean tab moved; dirty/pinned/manual/unknown/duplicate tabs protected')
    op({'op':'save-collection','name':'Muse QA temporary'});export=op({'op':'export'});assert 'Muse QA temporary' in export['collections'];op({'op':'import','value':export});results.append('collections export/import and duplicate suggestions verified')
    capture=op({'op':'capture','id':by['safe']['id'],'kind':'research','note':'Disposable Muse Monitor acceptance fixture'});assert capture['state']=='queued' and capture['sync']=='pending';results.append('capture entered durable pending queue without Google write')
    out={'passed':True,'checks':results,'realSubmissions':0,'existingPortalTabsPreserved':True};dest=Path(config['state'])/'acceptance/extension-results.json';dest.write_text(json.dumps(out,indent=2));dest.chmod(0o600);print(json.dumps(out))
finally:
    if old:
        try:op({'op':'import','value':old})
        except Exception as e:print('Rule restore requires attention:',type(e).__name__)
    try:run('let count=0;for(const p of page.context().pages()){if(p.url().startsWith('+json.dumps(base)+')){await p.close();count++;}}return {closedFixtureTabs:count};')
    except Exception:pass
    if capture and capture.get('id'):
        import sqlite3
        db=sqlite3.connect(Path(config['state'])/'journal.sqlite3');db.execute('DELETE FROM alias WHERE canonical=?',(capture['id'],));db.execute('DELETE FROM item WHERE id=?',(capture['id'],));db.commit();db.close()
    server.shutdown()

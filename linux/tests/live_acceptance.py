"""Explicit VM acceptance on disposable pages in the two NEW profiles.

Run as muse-main after the service is installed. Never navigates or closes a
pre-existing page, never uses Main's signed-in Jobs profile for a mutation.
"""
import hashlib,json,os,re,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.cli import UnixHTTP

config=json.loads((Path.home()/'.config/muse-monitor/config.json').read_text())
# Existing safe default uses the two original new profiles. A maintenance
# operator may explicitly select another pair after saving/closing user work.
selected=os.environ.get('MUSE_MONITOR_TEST_WORKSPACES','research,scholarships').split(',')
assert len(selected)==2 and len(set(selected))==2 and all(w in config['workspaces'] for w in selected),'Explicitly choose two active idle workspaces'
aliases=dict(zip(('research','scholarships'),selected))
events=[];leases={};pages={};task='muse-monitor-controlled-acceptance';grant=None;paused_by_test=False
def api(path,body=None,actor='main',expected=200):
    for alias,wid in aliases.items():
        prefix='/v1/workspaces/'+alias+'/'
        if path.startswith(prefix):path='/v1/workspaces/'+wid+'/'+path[len(prefix):];break
    token=Path(config['principalFiles'][actor]).read_text().strip()
    c=UnixHTTP(str(Path(config['runtime'])/'muse-monitor.sock'))
    headers={'Authorization':'Bearer '+token,'X-Muse-Protocol':'1'}
    if body is not None:headers['Content-Type']='application/json'
    started=time.time();c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
    r=c.getresponse();data=r.read();code=r.status;c.close()
    events.append({'route':path.split('?')[0],'started':started,'seconds':round(time.time()-started,3),'status':code})
    assert code==expected,(path,code,data[:200])
    return data if data.startswith(b'\x89PNG') else json.loads(data)
def worker(wid,op,expected=200,**body):return api('/v1/worker',dict(op=op,workspace=aliases[wid],**body),expected=expected)
def browser(wid,kind,expected=200,**fields):
    lease=leases[wid];page=pages.get(wid,{})
    return worker(wid,'browser',expected=expected,token=lease['token'],generation=lease['generation'],requestId=str(uuid.uuid4()),taskComplete=kind=='close',action=dict(kind=kind,page=page.get('id'),document=page.get('generation'),**fields))
def status(wid):return next(w for w in api('/v1/status',actor='windows-human')['workspaces'] if w['id']==aliases[wid])
def control(wid,kind,grant=None,expected=200):
    w=status(wid);p=pages[wid]
    return api(f'/v1/workspaces/{wid}/{kind}',{'requestId':str(uuid.uuid4()),'generation':w['generation'],'page':p['id'],'pageGeneration':p['generation'],'grant':grant},actor='windows-human',expected=expected)
class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
        self.wfile.write(b'''<!doctype html><title>Muse Monitor controlled acceptance</title>
<h1>Muse Monitor live fixture</h1><label>Diagnostic text <input id="entry"></label>
<button disabled>Unavailable test control</button>
<label>Diagnostic PDF <input type="file" id="file"></label><output id="receipt"></output>
<script>file.onchange=async()=>{const f=file.files[0];const d=await crypto.subtle.digest('SHA-256',await f.arrayBuffer());receipt.textContent=f.name+' '+f.size+' '+Array.from(new Uint8Array(d),b=>b.toString(16).padStart(2,'0')).join('');};</script>''')
    def log_message(self,*args):pass

server=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    until=time.monotonic()+45
    while time.monotonic()<until:
        try:
            observed=api('/v1/status',actor='windows-human')['workspaces']
            if all(w['pages'] for w in observed):break
        except (OSError,AssertionError):pass
        time.sleep(.25)
    else:raise AssertionError('Workspace startup readiness not observed')
    for wid in ('research','scholarships'):
        prior=status(wid);fixtures=[p for p in prior['pages'] if p.get('managed') and p.get('title')=='Muse Monitor controlled acceptance' and p.get('url','').startswith('http://127.0.0.1:')]
        only_test=all(p.get('url')=='about:blank' or p in fixtures for p in prior['pages'])
        if only_test and (prior['state'] in ('human','paused') or prior.get('task')==task):
            # Recover only this fixture's interrupted test using explicit human
            # authority. An agent never clears a human pause automatically.
            pages[wid]=fixtures[0] if fixtures else prior['pages'][0]
            if prior['state'] not in ('human','paused'):control(wid,'pause')
            g=control(wid,'takeover')['grant'];control(wid,'resume',g['token']);pages.pop(wid)
    for wid in ('research','scholarships'):
        leases[wid]=worker(wid,'claim',task=task)
        for previous in status(wid)['pages']:
            if previous.get('managed') and previous.get('title')=='Muse Monitor controlled acceptance' and previous.get('url','').startswith('http://127.0.0.1:'):
                pages[wid]=previous;browser(wid,'close');pages.pop(wid)
        pages[wid]=browser(wid,'new',url=f'http://127.0.0.1:{server.server_port}/?workspace={wid}')
    snap=browser('research','snapshot')['snapshot']
    disabled=re.search(r'button "Unavailable test control"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    rid=str(uuid.uuid4());failure=[]
    def stall():
        lease=leases['research'];p=pages['research']
        failure.append(worker('research','browser',expected=503,requestId=rid,token=lease['token'],generation=lease['generation'],action={'kind':'click','ref':disabled,'page':p['id'],'document':p['generation']}))
    t=threading.Thread(target=stall);t.start();time.sleep(.5)
    started=time.time();second=browser('scholarships','snapshot')['snapshot'];elapsed=time.time()-started
    assert 'Diagnostic text' in second and elapsed<5 and t.is_alive(),('No actual overlap',elapsed)
    entry=re.search(r'textbox "Diagnostic text"[^\n]*?\[ref=([A-Za-z0-9]+)\]',second).group(1)
    value=browser('scholarships','fill',ref=entry,text='Independent workspace progressed')['observed']
    assert value=='Independent workspace progressed' and t.is_alive()
    print('PASS real concurrent progress while the first workspace waits:',round(elapsed,3),'seconds',flush=True)
    t.join(30);assert not t.is_alive() and failure
    worker('research','reconcile',requestId=rid,evidence={'outcome':'disabled fixture button did not click','evidence':'controlled fixture has a permanently disabled button; second-workspace observation completed'})
    # Genuine new PDF: verify exact attached bytes inside the browser fixture.
    upload=re.search(r'button "Diagnostic PDF"[^\n]*?\[ref=([A-Za-z0-9]+)\]',second).group(1)
    artifact=Path(config['state'])/'artifacts/diagnostic-20261112.pdf';sha=hashlib.sha256(artifact.read_bytes()).hexdigest()
    result=browser('scholarships','upload',ref=upload,artifact=artifact.name)
    assert result['files'][0]['name']==artifact.name and result['files'][0]['size']==artifact.stat().st_size
    receipt=browser('scholarships','snapshot')['snapshot'];assert sha in receipt
    print('PASS PDF attachment filename, size and browser-side SHA-256',flush=True)
    for wid in ('research','scholarships'):
        l=leases[wid];worker(wid,'release',token=l['token'],generation=l['generation']);leases.pop(wid)
    wid='research';p=pages[wid]
    w=status(wid);body={'requestId':str(uuid.uuid4()),'generation':w['generation'],'page':p['id'],'pageGeneration':p['generation'],'action':{'kind':'text','text':'MUST NOT ENTER'}}
    api('/v1/workspaces/research/input',body,actor='windows-human',expected=403)
    control(wid,'pause');paused_by_test=True;grant=control(wid,'takeover')['grant']
    assert status(wid)['state']=='human'  # prior HTTP connections already closed
    # Direct raw input, bypassing WPF's disabled buttons, still needs this grant.
    body.update(requestId=str(uuid.uuid4()),generation=grant['generation'],grant=grant['token'],action={'kind':'key','key':'Tab'})
    api('/v1/workspaces/research/input',body,actor='windows-human')
    api('/v1/workspaces/research/input',body,actor='windows-human',expected=409)
    api('/v1/workspaces/scholarships/input',dict(body,requestId=str(uuid.uuid4())),actor='windows-human',expected=403)
    # Pausing Research does not pause the other workspace.
    leases['scholarships']=worker('scholarships','claim',task=task)
    browser('scholarships','snapshot')
    control(wid,'resume',grant['token'])
    paused_by_test=False
    api('/v1/workspaces/research/input',dict(body,requestId=str(uuid.uuid4())),actor='windows-human',expected=403)
    print('PASS Watch denial, exact human takeover, replay/cross-workspace/stale grant denial, independent pause/resume',flush=True)
finally:
    # Reacquire the same authenticated task identity to retire only its own
    # disposable pages. Pre-existing pages (including blank ones) remain intact.
    if paused_by_test and grant:
        control('research','resume',grant['token'])
    cleanup_errors=[]
    for wid,p in list(pages.items()):
        try:
            if wid not in leases:leases[wid]=worker(wid,'claim',task=task)
            browser(wid,'close')
        except Exception as error:cleanup_errors.append(type(error).__name__)
        finally:
            if wid in leases:
                try:l=leases[wid];worker(wid,'release',token=l['token'],generation=l['generation'])
                except Exception as error:cleanup_errors.append(type(error).__name__)
    server.shutdown()
    assert not cleanup_errors,cleanup_errors
out=Path(config['state'])/'acceptance';out.mkdir(mode=0o700,exist_ok=True)
(out/'live-results.json').write_text(json.dumps({'passed':True,'events':events,'actualConcurrentWorkspaces':True,'realSubmissions':0,'preexistingTabsPreserved':True},indent=2)+'\n')
print('PASS live VM authority/concurrency/attachment acceptance; disposable pages retired, leases released.',flush=True)

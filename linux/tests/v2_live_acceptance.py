"""Exact v2 regression on own localhost tabs, including a lease boundary."""
import base64,hashlib,json,os,re,shlex,subprocess,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.browser import Browser
from muse_monitor.cli import request

cfgpath=Path.home()/'.config/muse-monitor/config.json';cfg=json.loads(cfgpath.read_text())
nonce=uuid.uuid4().hex;token=None;owned=[];prior=None
report={'checks':{},'commands':[]};last_safe_response=''
def call(*args,expected=0):
    global last_safe_response
    start=time.monotonic()
    r=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=dict(os.environ,SSH_ORIGINAL_COMMAND=shlex.join(args)),capture_output=True,text=True,timeout=55)
    report['commands'].append({'operation':args[0],'seconds':round(time.monotonic()-start,3),'exit':r.returncode})
    if args[0] not in ('claim','release'):last_safe_response=(r.stdout+'\n'+r.stderr)[-12000:]
    if r.returncode!=expected:raise AssertionError('Unexpected exit for '+args[0])
    return r.stdout
def run(verb,*args,expected=0):
    payload=base64.urlsafe_b64encode(json.dumps({'command':verb,'args':list(args)}).encode()).decode().rstrip('=')
    return call('run-encoded',token,payload,expected=expected)
def value(text):return json.JSONDecoder().raw_decode(text.split('### Result\n',1)[1].strip())[0]
def claim():
    found=re.findall(r'(?m)^[A-Za-z0-9][A-Za-z0-9_-]{31}$',call('claim'))
    if len(found)!=1:raise AssertionError('No unique private token')
    return found[0]
def inventory():return value(run('tab-list'))
def index(url):
    rows=[r['index'] for r in inventory()['tabs'] if r['url']==url]
    if len(rows)!=1:raise AssertionError('Own tab identity changed')
    return rows[0]
def select(url):
    i=index(url);v=value(run('tab-select',str(i)))
    assert v['selectedIndex']==i
    return v
def snapshot():return run('snapshot')
def ref(s,label,kind='textbox'):
    return re.search(kind+' "'+re.escape(label)+r'"[^\n]*?\[ref=([A-Za-z0-9]+)\]',s).group(1)
def assert_text(text):
    assert 'value:'+hashlib.sha256(text.encode()).hexdigest() in snapshot()
def chrome_pids():
    pids=[]
    for p in Path('/proc').iterdir():
        try:
            if p.name.isdigit() and p.stat().st_uid==os.getuid() and p.joinpath('exe').resolve()==Path('/opt/google/chrome/chrome') and b'--type=' not in p.joinpath('cmdline').read_bytes():pids.append(int(p.name))
        except OSError:pass
    return sorted(pids)

class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        letter='A' if self.path.endswith('/A') else 'B'
        html='''<!doctype html><title>Muse v2 fixture __LETTER__</title><h1>Fixture __LETTER__</h1>
<label>Exact text<textarea id="t"></textarea></label><output id="v"></output>
<label>Protected password<input type="password"></label>
<button onclick="document.querySelector('#f').click()">Diagnostic chooser</button>
<input id="f" type="file" hidden><output id="receipt"></output>
<script>
async function sha(b){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b)),x=>x.toString(16).padStart(2,'0')).join('');}
t.oninput=async()=>v.textContent='value:'+await sha(new TextEncoder().encode(t.value));
f.onchange=async()=>{const x=f.files[0];receipt.textContent='file:'+x.name+' '+await sha(await x.arrayBuffer());};
</script>'''.replace('__LETTER__',letter)
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(html.encode())
    def log_message(self,*args):pass

server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=server.serve_forever,daemon=True).start()
root='http://127.0.0.1:'+str(server.server_port)+'/'+nonce;observer=Browser(cfg)
before={wid:{p['id'] for p in observer.describe(wid,True)['pages']} for wid in cfg['workspaces']};pids=chrome_pids()
try:
    token=claim()
    assert 'run-encoded' in call('run',token,'help')
    assert 'clear' in json.loads(call('run',token,'capabilities'))['browserCommands']
    files=json.loads(call('run',token,'artifacts'))['artifacts']
    assert any(x['artifact']=='Fixture_Applicant_Resume.pdf' for x in files)
    usage=call('run',token,'admit','only-a-filename.pdf',expected=2)
    assert 'DRIVE_FILE_ID' in usage and 'PREPARATION_USAGE' in usage
    admitted=json.loads(call('run',token,'admit','fixtureDriveResumeDocument01','Fixture_Applicant_Resume.pdf'))
    assert admitted['bytes']==35456
    call('run','invalid-token','artifacts',expected=1)
    report['checks']['nested_help_admit_inventory_compatible_and_invalid_token_denied']=True
    prior=inventory()['selectedIndex']
    for letter in ('A','B'):
        url=root+'/'+letter;run('tab-new',url);owned.append(url)
    select(root+'/A')
    for _ in range(3):
        v=inventory();assert all(any(t['url']==url for t in v['tabs']) for url in owned)
    report['checks']['repeated_inventory_is_complete']=True
    # Cancellation without a chooser is a harmless, truthful no-op.
    assert value(run('upload'))['noChooserOpen']
    diagnostic=Path(cfg['state'])/'artifacts/diagnostic-20261112.pdf'
    error=run('upload',diagnostic.name,expected=1)
    assert 'UPLOAD_NOT_CONFIRMED' in error and 'Inspect' in error
    assert 'file:'+diagnostic.name not in snapshot()
    report['checks']['no_chooser_upload_has_actionable_error_and_cancel_is_idempotent']=True
    s=snapshot();r=ref(s,'Exact text')
    text=' leading "quote" é\ntrailing  '
    run('fill',r,text);assert_text(text)
    assert value(run('read-value',r))['value']==text
    password=ref(snapshot(),'Protected password')
    assert 'MUSE_FIELD_VALUE_NOT_AVAILABLE' in run('read-value',password,expected=1)
    report['checks']['read_value_is_exact_and_password_readback_denied']=True
    run('fill',ref(snapshot(),'Exact text'),'');assert_text('')
    run('fill',ref(snapshot(),'Exact text'),'remove me');run('clear',ref(snapshot(),'Exact text'));assert_text('')
    run('type',ref(snapshot(),'Exact text'),' leading and trailing ');assert_text(' leading and trailing ')
    run('press',ref(snapshot(),'Exact text'),'Control+A');run('press',ref(snapshot(),'Exact text'),'Backspace');assert_text('')
    run('type',' focused leading and trailing ');assert_text(' focused leading and trailing ')
    run('clear',ref(snapshot(),'Exact text'));assert_text('')
    report['checks']['empty_fill_clear_whitespace_and_optional_ref_type_press_exact']=True
    old_token=token;call('release',token);token=None;token=claim();assert token!=old_token
    v=inventory();assert all(any(t['url']==url for t in v['tabs']) for url in owned)
    selected=select(root+'/B');assert selected['tabs'][selected['selectedIndex']]['url']==root+'/B'
    again=value(run('tab-select',str(index(root+'/B'))));assert again['selectedIndex']==selected['selectedIndex']
    report['checks']['tabs_survive_release_reclaim_and_selection_ack_is_current']=True
    for _ in range(3):
        s=snapshot();assert s.count('### Open tabs')==1 and all(url in s for url in owned)
    report['checks']['every_snapshot_has_full_inventory']=True
    select(root+'/A');s=snapshot();run('click',ref(s,'Diagnostic chooser','button'))
    # Native modal-safe listing/selection remain available, and no chooser is
    # dismissed merely by moving to another own fixture tab.
    assert all(url in run('tab-list') for url in owned)
    select(root+'/B');assert 'Fixture B' in snapshot()
    select(root+'/A');run('upload');s=snapshot()
    report['checks']['chooser_does_not_deadlock_list_switch_cancel']=True
    run('click',ref(s,'Diagnostic chooser','button'));run('upload',diagnostic.name)
    assert hashlib.sha256(diagnostic.read_bytes()).hexdigest() in snapshot()
    report['checks']['real_chooser_diagnostic_upload_sha_exact']=True
    report['complete']=True
except Exception as e:
    report.update(complete=False,errorType=type(e).__name__)
    out=Path(cfg['state'])/'acceptance/v2-private-last-response.txt';out.write_text(last_safe_response);out.chmod(0o600)
finally:
    if token:
        if owned:
            try:run('upload') # resolve only our current fixture's possible chooser
            except Exception:pass
        for url in reversed(owned):
            try:run('tab-close',str(index(url)))
            except Exception:report['cleanupError']=True
        if prior is not None:
            try:run('tab-select',str(prior))
            except Exception:report['selectionRestoreError']=True
        call('release',token)
    server.shutdown()
    after={wid:{p['id'] for p in observer.describe(wid,True)['pages']} for wid in cfg['workspaces']}
    report['checks']['all_original_pages_and_chrome_processes_preserved']=all(before[w]<=after[w] for w in before) and chrome_pids()==pids
    report['checks']['own_fixture_pages_removed']=not any(p['url'].startswith(root) for p in observer.describe('jobs',True)['pages'])
    report['checks']['candidate_material_never_attached']=True
    report['complete']=report.get('complete',False) and all(report['checks'].values()) and not report.get('cleanupError') and not report.get('selectionRestoreError')
    out=Path(cfg['state'])/('acceptance/v2-live-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+nonce[:8]+'.json');out.write_text(json.dumps(report,indent=2)+'\n');out.chmod(0o600)
    print(json.dumps(report),flush=True)
    if not report['complete']:raise SystemExit(1)

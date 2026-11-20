"""Opt-in own-tab popup/upload checks through the unchanged legacy command shape.

No employer page is clicked and only the diagnostic PDF is attached locally.
"""
import hashlib,json,os,re,subprocess,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.browser import Browser

cfg=json.loads((Path.home()/'.config/muse-monitor/config.json').read_text())
observer=Browser(cfg);nonce=uuid.uuid4().hex;token=None;prior=None
report={'checks':{},'commands':[]}
def call(*args):
    # OpenSSH's old raw joining, intentionally no new helper or encoded mode.
    assert all(not re.search(r"[\s'\"]",s) for s in args)
    start=time.monotonic()
    r=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=os.environ|{'SSH_ORIGINAL_COMMAND':' '.join(args)},capture_output=True,text=True,timeout=50)
    report['commands'].append({'command':args[2] if args[0]=='run' else args[0],'ms':round((time.monotonic()-start)*1000,2),'exit':r.returncode})
    if r.returncode:raise AssertionError('Handler failed: '+args[0])
    return r.stdout
def run(*args):return call('run',token,*args)
def value(out):return json.JSONDecoder().raw_decode(out.split('### Result\n',1)[1].strip())[0]
def tabs():return value(run('tab-list'))
def select(url):
    rows=[r for r in tabs()['tabs'] if r['url']==url];assert len(rows)==1
    run('tab-select',str(rows[0]['index']))
def ref(label,role='button'):
    return re.search(role+' "'+re.escape(label)+r'"[^\n]*?\[ref=([A-Za-z0-9]+)\]',run('snapshot')).group(1)
def pids():
    result=[]
    for p in Path('/proc').iterdir():
        try:
            if p.name.isdigit() and p.stat().st_uid==os.getuid() and p.joinpath('exe').resolve()==Path('/opt/google/chrome/chrome') and b'--type=' not in p.joinpath('cmdline').read_bytes():result.append(int(p.name))
        except OSError:pass
    return sorted(result)
class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body='''<!doctype html><title>Muse popup and upload fixture</title>
<a href="./linked" target="_blank" rel="noopener">Open linked fixture</a>
<button onclick="window.open('./scripted','_blank')">Open scripted fixture</button>
<button onclick="steps.showModal()">Open staged fixture</button>
<dialog id="steps" aria-labelledby="stepTitle"><h2 id="stepTitle">Step 1: Fixture documents</h2>
<button onclick="window.open('./staged','_blank');steps.close()">Step 2: Fixture external page</button>
<button onclick="steps.close()">Cancel fixture</button></dialog>
<button onclick="document.querySelector('#f').click()">Choose diagnostic PDF</button>
<input type="file" id="f" hidden><output id="receipt"></output>
<script>f.onchange=async()=>{const x=f.files[0];receipt.textContent=x.name+' '+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await x.arrayBuffer())),v=>v.toString(16).padStart(2,'0')).join('');};</script>'''
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body.encode())
    def log_message(self,*args):pass
http=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=http.serve_forever,daemon=True).start()
root='http://127.0.0.1:'+str(http.server_port)+'/'+nonce+'/'
before={w:{p['id'] for p in observer.describe(w,True)['pages']} for w in cfg['workspaces']};before_pids=pids()
try:
    report['handler']=json.loads(call('capabilities'))['revision']
    files=json.loads(call('artifacts'))['artifacts']
    report['checks']['universal_resume_is_admitted']=any(x['artifact']=='Fixture_Applicant_Resume.pdf' and x['bytes']==35456 for x in files)
    token=call('claim').strip();assert re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{31}',token)
    prior=tabs()['selectedIndex'];run('tab-new',root)
    for suffix,label,role in [('linked','Open linked fixture','link'),('scripted','Open scripted fixture','button')]:
        select(root);run('click',ref(label,role));deadline=time.monotonic()+4
        while True:
            found=any(t['url']==root+suffix for t in tabs()['tabs'])
            if found or time.monotonic()>=deadline:break
            time.sleep(.1)
        assert found,'Own popup did not appear'
        select(root+suffix);assert 'Muse popup and upload fixture' in run('snapshot')
        report['checks'][suffix+'_popup_discovered_and_selectable']=True
    select(root);run('click',ref('Open staged fixture'))
    snap=run('snapshot');prefix=snap.split('### Snapshot\n',1)[0]
    assert '### Page dialogs' in prefix and 'Step 1: Fixture documents' in prefix and 'Step 2: Fixture external page' in prefix
    report['checks']['staged_dialog_is_prominent_before_full_snapshot']=True
    step_ref=re.search(r'button "Step 2: Fixture external page"[^\n]*?\[ref=([A-Za-z0-9]+)\]',prefix).group(1)
    run('click',step_ref);select(root+'staged')
    assert 'Muse popup and upload fixture' in run('snapshot')
    report['checks']['staged_external_step_uses_unchanged_fresh_ref']=True
    select(root);run('click',ref('Choose diagnostic PDF'));run('upload')
    report['checks']['chooser_cancel_without_human']=True
    run('click',ref('Choose diagnostic PDF'));run('upload','diagnostic-20261112.pdf')
    diagnostic=Path(cfg['state'])/'artifacts/diagnostic-20261112.pdf'
    assert hashlib.sha256(diagnostic.read_bytes()).hexdigest() in run('snapshot')
    report['checks']['existing_command_shape_upload_exact_sha']=True
    report['checks']['real_resume_never_attached']=True
    report['complete']=True
finally:
    if token:
        # Only nonce-owned pages, closing from the highest current index.
        for row in sorted((t for t in tabs()['tabs'] if t['url'].startswith(root)),key=lambda x:x['index'],reverse=True):
            run('tab-close',str(row['index']))
        if prior is not None:run('tab-select',str(prior))
        call('release',token)
    http.shutdown()
    after={w:{p['id'] for p in observer.describe(w,True)['pages']} for w in cfg['workspaces']}
    report['checks']['original_pages_and_processes_preserved']=all(before[w]<=after[w] for w in before) and pids()==before_pids
    report['checks']['own_pages_removed']=not any(p['url'].startswith(root) for p in observer.describe('jobs',True)['pages'])
    report['complete']=report.get('complete',False) and all(report['checks'].values())
    print(json.dumps(report),flush=True)
    if not report['complete']:raise SystemExit(1)

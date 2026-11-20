"""Attended repair fixture: two own tabs, diagnostic upload, no portal changes."""
import base64,hashlib,json,os,re,shlex,subprocess,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.cli import request
from muse_monitor.browser import Browser

cfgpath=Path.home()/'.config/muse-monitor/config.json'
cfg=json.loads(cfgpath.read_text());nonce=uuid.uuid4().hex;token=None
report={'checks':{},'commands':[]};own_urls=[];prior=None

def remote(*args,expected=0):
    env=dict(os.environ,SSH_ORIGINAL_COMMAND=shlex.join(args))
    started=time.monotonic()
    r=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=env,capture_output=True,text=True,timeout=50)
    report['commands'].append({'command':args[0] if args[0]!='run' else args[2],
                               'seconds':round(time.monotonic()-started,3),'exit':r.returncode})
    if r.returncode!=expected:raise AssertionError('Restricted command failed: '+args[0])
    return r.stdout

def run(command,*args):
    payload=base64.urlsafe_b64encode(json.dumps({'command':command,'args':list(args)}).encode()).decode().rstrip('=')
    return remote('run-encoded',token,payload)

def index_for(url):
    listing=run('tab-list')
    rows=[int(m.group(1)) for m in re.finditer(r'(?m)^- (\d+):.*$',listing) if url in m.group(0)]
    if len(rows)!=1:raise AssertionError('Own fixture tab identity changed')
    return rows[0]

def browser_pids():
    pids=[]
    for p in Path('/proc').iterdir():
        try:
            if p.name.isdigit() and p.stat().st_uid==os.getuid() and p.joinpath('exe').resolve()==Path('/opt/google/chrome/chrome') and b'--type=' not in p.joinpath('cmdline').read_bytes():pids.append(int(p.name))
        except OSError:pass
    return sorted(pids)

class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        letter='A' if self.path.endswith('/A') else 'B'
        html='''<!doctype html><title>Muse handoff fixture __LETTER__</title>
<h1>Diagnostic workspace tab __LETTER__</h1><label>Diagnostic text<textarea></textarea></label>
<button onclick="document.querySelector('input').click()">Diagnostic chooser</button>
<input type="file" hidden><output></output><script>
document.querySelector('input').onchange=async e=>{const f=e.target.files[0];document.querySelector('output').textContent='diagnostic:'+f.name+' '+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer())),x=>x.toString(16).padStart(2,'0')).join('');};
</script>'''.replace('__LETTER__',letter)
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(html.encode())
    def log_message(self,*args):pass

http=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
threading.Thread(target=http.serve_forever,daemon=True).start()
observer=Browser(cfg);before_ids={p['id'] for p in observer.describe('jobs',True)['pages']};before_pids=browser_pids()
try:
    cap=json.loads(remote('capabilities'))
    assert cap['tabSwitch'] and 'tab-select' in cap['browserCommands'] and not cap['arbitraryShell']
    report['checks']['installed_capabilities_describe_tab_switch_and_import']=True
    # Idempotent admission of the actual, existing canonical PDF. Never attach
    # applicant material to the diagnostic form or any employer portal.
    admitted=json.loads(remote('admit','fixtureDriveResumeDocument01','Fixture_Applicant_Resume.pdf'))
    inventory=json.loads(remote('artifacts'))
    assert any(r['artifact']==admitted['artifact'] and r['sha256']==admitted['sha256'] for r in inventory['artifacts'])
    report['checks']['actual_universal_resume_admitted_and_discoverable']=True
    claim=remote('claim');found=re.findall(r'(?m)^[A-Za-z0-9][A-Za-z0-9_-]{31}$',claim)
    assert len(found)==1;token=found[0]
    selected=re.findall(r'(?m)^- (\d+): \(current\)',run('tab-list'))
    prior=int(selected[0]) if len(selected)==1 else None
    root='http://127.0.0.1:'+str(http.server_port)+'/'+nonce
    for letter in ('A','B'):
        url=root+'/'+letter;run('tab-new',url);own_urls.append(url)
    run('tab-select',str(index_for(root+'/A')))
    assert 'Diagnostic workspace tab A' in run('snapshot')
    run('tab-switch',str(index_for(root+'/B')))
    assert 'Diagnostic workspace tab B' in run('snapshot')
    run('switch-tab',str(index_for(root+'/A')))
    assert 'Diagnostic workspace tab A' in run('snapshot')
    report['checks']['canonical_tab_select_and_both_aliases_switch_exact_tabs']=True
    snap=run('snapshot');ref=re.search(r'button "Diagnostic chooser"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    run('click',ref);run('upload')
    snap=run('snapshot');assert 'Diagnostic workspace tab A' in snap
    report['checks']['native_chooser_cancel_recovers_snapshot_without_human']=True
    ref=re.search(r'button "Diagnostic chooser"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    diagnostic=Path(cfg['state'])/'artifacts/diagnostic-20261112.pdf'
    sha=hashlib.sha256(diagnostic.read_bytes()).hexdigest()
    run('click',ref);run('upload',diagnostic.name)
    assert sha in run('snapshot')
    report['checks']['diagnostic_pdf_attaches_with_exact_sha']=True
    report['checks']['candidate_resume_not_uploaded_as_fixture']=True
    report['complete']=True
except Exception as e:report.update(complete=False,errorType=type(e).__name__)
finally:
    if token:
        for url in reversed(own_urls):
            try:run('tab-close',str(index_for(url)))
            except Exception:report['cleanupError']=True
        if prior is not None:
            try:run('tab-select',str(prior))
            except Exception:report['selectionRestoreError']=True
        remote('release',token)
    http.shutdown()
    report['checks']['original_main_pages_preserved']=before_ids<={p['id'] for p in observer.describe('jobs',True)['pages']}
    report['checks']['same_chrome_processes_preserved']=before_pids==browser_pids()
    report['checks']['own_fixture_pages_removed']=not any(p['url'].startswith(root) for p in observer.describe('jobs',True)['pages']) if 'root' in globals() else True
    report['complete']=report.get('complete',False) and all(report['checks'].values()) and not report.get('cleanupError') and not report.get('selectionRestoreError')
    out=Path(cfg['state'])/'acceptance/20261114-handoff-live.json';out.write_text(json.dumps(report,indent=2)+'\n');out.chmod(0o600)
    print(json.dumps(report),flush=True)
    if not report['complete']:raise SystemExit(1)

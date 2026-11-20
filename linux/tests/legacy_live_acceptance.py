"""Opt-in restricted-handler repair acceptance, run as muse-main.

Uses one new localhost page and a new disposable diagnostic Drive PDF. Never
changes an existing portal form or uploads candidate materials as a fixture.
"""
import base64,fcntl,hashlib,json,os,re,subprocess,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.cli import request,UnixHTTP
from muse_monitor.google_bridge import Google

cfgpath=Path.home()/'.config/muse-monitor/config.json'
cfg=json.loads(cfgpath.read_text());run=uuid.uuid4().hex
report={'checks':{},'commands':[],'runtimeRevision':cfg['revision']}
token=None;drive_id=None;artifact=None;fixture=None;stop=threading.Event();viewer=None
api=Google(cfg['google']['token'])

def encode(data):return base64.urlsafe_b64encode(json.dumps(data,ensure_ascii=True).encode()).decode().rstrip('=')
def remote(*args,expected=0):
    env=os.environ.copy();env['SSH_ORIGINAL_COMMAND']=' '.join(__import__('shlex').quote(x) for x in args)
    start=time.monotonic()
    r=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=env,text=True,capture_output=True,timeout=55)
    report['commands'].append({'command':args[0] if args[0]!='run' else args[2],'seconds':round(time.monotonic()-start,3),'exit':r.returncode})
    assert r.returncode==expected,('Handler exit mismatch',args[0],r.returncode)
    return r.stdout
def run_command(command,*args):return remote('run-encoded',token,encode({'command':command,'args':list(args)}))
def parse_result(text):return json.JSONDecoder().raw_decode(text.split('### Result\n',1)[-1].strip())[0]
def chrome_pids():
    ids=[]
    for p in Path('/proc').iterdir():
        try:
            if p.name.isdigit() and p.stat().st_uid==os.getuid() and p.joinpath('exe').resolve()==Path('/opt/google/chrome/chrome'):
                args=p.joinpath('cmdline').read_bytes()
                if b'--type=' not in args:ids.append(int(p.name))
        except OSError:pass
    return sorted(ids)

class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
        self.wfile.write(b'''<!doctype html><title>Muse restricted upload acceptance</title>
<label>Diagnostic text<textarea id="text"></textarea></label><output id="typed"></output>
<button onclick="document.querySelector('#pdf').click()">Open diagnostic file chooser</button>
<input type="file" id="pdf" hidden><output id="receipt"></output>
<script>
async function sha(bytes){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');}
document.querySelector('#text').oninput=async e=>document.querySelector('#typed').textContent='text:'+await sha(new TextEncoder().encode(e.target.value));
document.querySelector('#pdf').onchange=async e=>{const f=e.target.files[0];document.querySelector('#receipt').textContent='file:'+f.name+' '+f.size+' '+await sha(await f.arrayBuffer());};
</script>''')
    def log_message(self,*args):pass

http=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=http.serve_forever,daemon=True).start()
fixture_url='http://127.0.0.1:'+str(http.server_port)+'/'+run
before_pids=chrome_pids();before_status=request(cfgpath,{'op':'status'})
before_pages={w['id']:{p['id'] for p in w['pages']} for w in before_status['workspaces']}

try:
    claim=remote('claim');tokens=re.findall(r'(?m)^[A-Za-z0-9_-]{32}$',claim)
    assert len(tokens)==1;token=tokens[0]
    report['checks']['new_token_starts_alphanumeric']=token[0].isalnum()
    original=remote('run',token,'tab-list');selected=re.findall(r'(?m)^- (\d+): \(current\)',original)
    prior_index=int(selected[0]) if len(selected)==1 else None
    run_command('tab-new',fixture_url);fixture=True
    snap=run_command('snapshot')
    ref=re.search(r'textbox "Diagnostic text"[^\n]*\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    value='Two words, "quotes", unicode \u00e9\n$(literal text; never shell)'
    run_command('fill',ref,value)
    assert 'text:'+hashlib.sha256(value.encode()).hexdigest() in run_command('snapshot')
    report['checks']['lossless_multiline_quoted_unicode_fill']=True
    env=os.environ.copy();env['SSH_ORIGINAL_COMMAND']='run '+token+' fill '+ref+' B.S. Cybersecurity'
    older=subprocess.run(['/usr/local/bin/muse-browser','--ssh'],env=env,text=True,capture_output=True,timeout=55)
    assert older.returncode==0
    assert 'text:'+hashlib.sha256(b'B.S. Cybersecurity').hexdigest() in run_command('snapshot')
    report['checks']['older_unquoted_multiword_client_compatible']=True
    remote('run',token,'run-code','not permitted',expected=1)
    remote('run',token,'upload',str(Path.home()/'.config/muse-monitor/google-token.json'),expected=1)
    report['checks']['arbitrary_code_and_credential_upload_denied']=True

    pdf=(Path(cfg['state'])/'acceptance/google-sandbox.pdf').read_bytes()
    sha=hashlib.sha256(pdf).hexdigest()
    drive_id=api.call('drive','files?fields=id','POST',{'name':'Muse Monitor disposable upload repair '+run+'.pdf','mimeType':'application/pdf'})['id']
    response=api.session.patch('https://www.googleapis.com/upload/drive/v3/files/'+drive_id+'?uploadType=media',data=pdf,headers={'Content-Type':'application/pdf'},timeout=15,allow_redirects=False)
    assert response.status_code==200
    artifact='repair-fixture-'+run+'.pdf'
    imported=json.loads(remote('monitor-encoded',encode({'op':'artifact-import','fileId':drive_id,'filename':artifact,'sha256':sha})))
    assert imported['sha256']==sha and imported['bytes']==len(pdf)
    report['checks']['drive_pdf_import_byte_exact']=True
    snap=run_command('snapshot');button=re.search(r'button "Open diagnostic file chooser"[^\n]*\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    run_command('click',button)
    run_command('upload',artifact)
    assert sha in run_command('snapshot')
    report['checks']['restricted_upload_chooser_receives_exact_pdf']=True

    # Real HUD frame polling competes with actual restricted text commands.
    status=request(cfgpath,{'op':'status'});page=next(p for w in status['workspaces'] if w['id']=='jobs' for p in w['pages'] if p['url']==fixture_url)
    workspace=next(w for w in status['workspaces'] if w['id']=='jobs')
    frame_codes=[]
    def watch():
        while not stop.is_set():
            try:
                c=UnixHTTP(str(Path(cfg['runtime'])/'muse-monitor.sock'))
                key=Path(cfg['principalFiles']['windows-human']).read_text().strip()
                route='/v1/workspaces/jobs/pages/'+page['id']+'/frame?generation='+str(workspace['generation'])+'&document='+page['generation']
                c.request('GET',route,headers={'Authorization':'Bearer '+key,'X-Muse-Protocol':'1'})
                r=c.getresponse();r.read();frame_codes.append(r.status);c.close()
            except OSError:frame_codes.append('connection-error')
            stop.wait(.1)
    viewer=threading.Thread(target=watch);viewer.start()
    for number in range(6):
        snap=run_command('snapshot');ref=re.search(r'textbox "Diagnostic text"[^\n]*\[ref=([A-Za-z0-9]+)\]',snap).group(1)
        text='Viewer contention pass '+str(number)
        run_command('fill',ref,text)
        assert 'text:'+hashlib.sha256(text.encode()).hexdigest() in run_command('snapshot')
    stop.set();viewer.join(10);assert not viewer.is_alive()
    assert 200 in frame_codes and all(c in (200,409) for c in frame_codes)
    report['frameStatusCounts']={str(c):frame_codes.count(c) for c in set(frame_codes)}
    report['checks']['six_verified_fills_during_real_viewer_polling_no_busy_failures']=True
    report['complete']=True
except Exception as exc:
    report['complete']=False;report['errorType']=type(exc).__name__
finally:
    stop.set()
    if viewer:viewer.join(10)
    if token:
        if fixture:
            listing=remote('run',token,'tab-list')
            indexes=[int(m.group(1)) for m in re.finditer(r'(?m)^- (\d+):.*$',listing) if fixture_url in m.group(0)]
            if len(indexes)==1:
                run_command('tab-close',str(indexes[0]));fixture=False
                if prior_index is not None:run_command('tab-select',str(prior_index))
        remote('release',token)
    if drive_id:report['driveFixtureTrashed']=api.call('drive','files/'+drive_id+'?fields=id,trashed','PATCH',{'trashed':True}).get('trashed') is True
    if artifact:
        target=Path(cfg['state'])/'artifacts'/artifact
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==sha:target.unlink()
        manifest=Path(cfg['state'])/'artifact-sources'/(sha+'.json')
        if manifest.is_file() and json.loads(manifest.read_text())['artifact']==artifact:manifest.unlink()
    http.shutdown()
    report['checks']['same_chrome_processes_preserved']=chrome_pids()==before_pids
    after=request(cfgpath,{'op':'status'})
    report['checks']['original_pages_preserved']=all(ids<={p['id'] for w in after['workspaces'] if w['id']==wid for p in w['pages']} for wid,ids in before_pages.items())
    report['workspaces']=[{'id':w['id'],'state':w['state'],'actor':w['actor'],'blocker':w['blocker']} for w in after['workspaces']]
    report['checks']['temporary_page_removed']=not fixture
    out=Path(cfg['state'])/'acceptance/20261113-legacy-repairs.json';out.write_text(json.dumps(report,indent=2));out.chmod(0o600)
    print(json.dumps(report),flush=True)

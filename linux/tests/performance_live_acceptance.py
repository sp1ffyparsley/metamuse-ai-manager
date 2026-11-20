"""One disposable Secondary page; preserve the existing Main worker/lease."""
import hashlib,json,time,uuid,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from muse_monitor.cli import request
from muse_monitor.browser import Browser

p=Path.home()/'.config/muse-monitor/config.json';config=json.loads(p.read_text());wid='scholarships'
run='performance-'+uuid.uuid4().hex;lease=None;page=None
report={'checks':{},'durationsMs':[]}
def call(op,**fields):return request(p,dict(op=op,workspace=wid,**fields))
def browser(kind,**fields):
    start=time.monotonic()
    result=call('browser',token=lease['token'],generation=lease['generation'],requestId=str(uuid.uuid4()),taskComplete=kind=='close',action=dict(kind=kind,**({} if page is None else {'page':page['id'],'document':page['generation']}),**fields))
    report['durationsMs'].append({'command':kind,'ms':round((time.monotonic()-start)*1000,2)})
    return result
class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
        self.wfile.write(b'''<!doctype html><title>Muse performance fixture</title><label>Diagnostic text<input id="text"></label>
<label>Diagnostic PDF<input type="file" id="file"></label><output id="receipt"></output>
<script>file.onchange=async()=>{const f=file.files[0];receipt.textContent=f.name+' '+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer())),x=>x.toString(16).padStart(2,'0')).join('');};</script>''')
    def log_message(self,*args):pass
http=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=http.serve_forever,daemon=True).start()
main_lease=Path(config['runtime'])/'muse-browser-main/lease'
before_main=hashlib.sha256(main_lease.read_bytes()).hexdigest() if main_lease.exists() else None
observer=Browser(config)
before=call('status');before_ids={x['id'] for x in observer.describe(wid,True)['pages']}
before_summary=call('diagnostics')
try:
    lease=call('claim',task=run)
    page=browser('new',url='http://127.0.0.1:'+str(http.server_port)+'/'+run)
    import re
    snap=browser('snapshot')['snapshot'];ref=re.search(r'textbox "Diagnostic text"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    private_value='private-fixture-value-'+run
    result=browser('fill',ref=ref,text=private_value);assert result['observed']==private_value
    report['checks']['exact_fill_over_fast_daemon']=True
    snap=browser('snapshot')['snapshot'];ref=re.search(r'button "Diagnostic PDF"[^\n]*?\[ref=([A-Za-z0-9]+)\]',snap).group(1)
    artifact=Path(config['state'])/'artifacts/diagnostic-20261112.pdf';sha=hashlib.sha256(artifact.read_bytes()).hexdigest()
    result=browser('upload',ref=ref,artifact=artifact.name);assert result['files'][0]['size']==artifact.stat().st_size
    assert sha in browser('snapshot')['snapshot']
    report['checks']['exact_pdf_upload_over_fast_daemon']=True
    browser('close');page=None
    call('release',token=lease['token'],generation=lease['generation']);lease=None
    summary=call('diagnostics');report['diagnosticSummary']=summary
    assert summary['events'].get('lease_start',0)>before_summary['events'].get('lease_start',0)
    assert summary['events'].get('lease_end',0)>before_summary['events'].get('lease_end',0)
    assert summary['commandCount']>before_summary['commandCount']
    traces=''.join(x.read_text() for x in (Path(config['state'])/'diagnostics').glob('events.jsonl*'))
    assert private_value not in traces and lease is None
    report['checks']['automatic_correlated_private_run_log']=True
    report['complete']=True
except Exception as exc:report.update(complete=False,errorType=type(exc).__name__)
finally:
    if page:
        try:browser('close')
        except Exception:pass
    if lease:
        try:call('release',token=lease['token'],generation=lease['generation'])
        except Exception:pass
    http.shutdown()
    after=call('status')
    report['checks']['secondary_original_pages_preserved']=before_ids<={x['id'] for x in observer.describe(wid,True)['pages']}
    # The real Main worker may renew/release its lease naturally. We never claim,
    # release or write Main's file in this acceptance test.
    report['checks']['only_secondary_claimed']=True
    report['mainWorkspace']='Active independent worker; page changes are not attributed to this Secondary-only fixture'
    out=Path(config['state'])/'acceptance/performance-live.json';out.write_text(json.dumps(report,indent=2));out.chmod(0o600)
    print(json.dumps(report))

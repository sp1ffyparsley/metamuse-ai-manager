"""Run under xvfb-run + private dbus. Real GTK picker, no user's browser/profile."""
import hashlib,json,os,re,subprocess,tempfile,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from muse_monitor.desktop_mode import apply_mode

report={'checks':{}};node='/opt/muse/node/bin/playwright-cli';session='p'+uuid.uuid4().hex[:6]
class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        html='''<title>Private picker regression</title><button onclick="f.click()">Choose test file</button><input id="f" type="file" hidden><output id="receipt"></output><script>f.onchange=async()=>{const x=f.files[0];receipt.textContent=x.name+' '+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await x.arrayBuffer())),v=>v.toString(16).padStart(2,'0')).join('');}</script>'''
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(html.encode())
    def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=server.serve_forever,daemon=True).start()
url='http://127.0.0.1:'+str(server.server_port)+'/'
with tempfile.TemporaryDirectory(prefix='muse-native-picker-') as folder:
    root=Path(folder);env=os.environ|{'XDG_CACHE_HOME':str(root/'cache'),'PATH':'/opt/muse/node/bin:/usr/bin:/bin','XDG_SESSION_TYPE':'x11','WAYLAND_DISPLAY':''}
    conf=root/'config.json';conf.write_text(json.dumps({'browser':{'launchOptions':{'executablePath':'/opt/google/chrome/chrome','chromiumSandbox':True,'args':['--ozone-platform=x11'],'ignoreDefaultArgs':['--disable-blink-features=AutomationControlled','--enable-unsafe-swiftshader','--unsafely-disable-devtools-self-xss-warnings']}},'outputMode':'stdout'}))
    def cli(*args):
        r=subprocess.run([node,'-s='+session,*args],env=env,cwd=root,text=True,capture_output=True,timeout=20)
        if '### Error' in r.stdout and not r.returncode:r.returncode=1
        return r
    def ok(*args):
        r=cli(*args)
        if r.returncode:raise AssertionError('Private CLI failed: '+args[0]+' '+(r.stdout+r.stderr)[-1100:])
        return r.stdout
    def value(out):return json.JSONDecoder().raw_decode(out.split('### Result\n',1)[1].strip())[0]
    def windows():
        r=subprocess.run(['xdotool','search','--onlyvisible','--name','.*'],env=env,capture_output=True,text=True)
        return set(r.stdout.split())
    def button():return re.search(r'button "Choose test file"[^\n]*\[ref=([A-Za-z0-9]+)\]',ok('snapshot')).group(1)
    try:
        ok('open',url,'--headed','--profile='+str(root/'profile'),'--config='+str(conf))
        ok('click',button())
        result=apply_mode(cli,True);assert result.returncode==0
        report['checks']['pending_intercept_switches_to_human_without_file_change']=value(result.stdout)['filesChanged'] is False
        ok('tab-new',url)
        info=value(ok('run-code','async p => ({listeners:p.listeners("filechooser").length,webDriver:await p.evaluate(()=>navigator.webdriver)})'))
        assert info['listeners']==0 and info['webDriver'] is True
        report['checks']['new_human_tabs_have_native_picker_without_hiding_automation']=True
        baseline=windows();ref=button()
        click=subprocess.Popen([node,'-s='+session,'click',ref],env=env,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+10;added=set()
        while time.monotonic()<deadline:
            added=windows()-baseline
            if added:break
            time.sleep(.1)
        assert added,'Native GTK picker window missing'
        selected=None
        for window in added:
            title=subprocess.run(['xdotool','getwindowname',window],env=env,capture_output=True,text=True).stdout.strip()
            if re.search('open|file|select',title,re.I):selected=window;break
        assert selected,'No recognizable native picker title'
        report['pickerTitle']=title;report['stage']='native-file-selection'
        human=root/'human-diagnostic.pdf';human.write_bytes(b'%PDF-1.4\nHuman native fixture\n%%EOF')
        subprocess.run(['xdotool','windowfocus','--sync',selected],env=env,check=True,timeout=5)
        subprocess.run(['xdotool','key','--clearmodifiers','ctrl+l'],env=env,check=True)
        subprocess.run(['xdotool','type','--clearmodifiers','--delay','1',str(human)],env=env,check=True)
        subprocess.run(['xdotool','key','Return'],env=env,check=True)
        deadline=time.monotonic()+5
        while selected in windows() and time.monotonic()<deadline:time.sleep(.1)
        if selected in windows():
            subprocess.run(['xdotool','key','Return'],env=env,check=True)
        out,err=click.communicate(timeout=15)
        assert click.returncode==0,'Native click failed: '+(out+err)[-600:]
        report['stage']='native-file-receipt'
        observed=value(ok('run-code','async p => ({files:await p.locator("#f").evaluate(e=>Array.from(e.files,f=>f.name)),receipt:await p.locator("#receipt").textContent()})'))
        report['nativeReceipt']=observed
        assert hashlib.sha256(human.read_bytes()).hexdigest() in ok('snapshot'),'Native selection receipt missing'
        report['checks']['real_native_file_window_selects_diagnostic_pdf']=True
        assert apply_mode(cli,True).returncode==0
        assert apply_mode(cli,False).returncode==0
        assert apply_mode(cli,False).returncode==0
        counts=value(ok('run-code','async p => p.context().pages().map(x=>({count:x.listeners("filechooser").length}))'))
        assert all(x['count']==1 for x in counts)
        report['checks']['agent_resume_restores_exactly_one_listener_per_page']=True
        agent=root/'agent-diagnostic.pdf';agent.write_bytes(b'%PDF-1.4\nAgent upload fixture\n%%EOF')
        ok('click',button());ok('upload',str(agent))
        assert hashlib.sha256(agent.read_bytes()).hexdigest() in ok('snapshot')
        report['checks']['agent_upload_still_works_after_human_native_picker']=True
        assert apply_mode(cli,True).returncode==0
        assert hashlib.sha256(agent.read_bytes()).hexdigest() in ok('snapshot')
        report['checks']['return_to_human_preserves_existing_attachment']=True
        report['checks']['production_profiles_never_opened']=True
        report['complete']=True
    except Exception as error:
        report.update(complete=False,errorType=type(error).__name__,error=str(error)[:1500])
    finally:
        cli('close');server.shutdown();print(json.dumps(report),flush=True)
        if not report.get('complete'):raise SystemExit(1)

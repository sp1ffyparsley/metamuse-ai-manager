"""Page-targeted adapter to the installed, patched Playwright CLI daemon."""
import contextlib,fcntl,json,os,re,subprocess,tempfile,threading,time
from pathlib import Path
from .store import Conflict

class BrowserError(Exception):pass
class Uncertain(BrowserError):pass

class Browser:
    def __init__(self,config):
        self.config=config;self.pid=os.getpid();self.locks={};self.cache={};self.frames={};self.input_modes={};self.mode_sessions={};self.last_timings={}
        self.source=Path(__file__).with_name('browser-code.js').read_text()
        for wid in config['workspaces']:self.locks[wid]=threading.RLock()

    def manual_login(self,wid):
        return (Path(self.config['workspaces'][wid]['base'])/'manual-login.json').exists()

    def paths(self,wid):
        spec=self.config['workspaces'][wid]
        runtime=Path(self.config['runtime'])/('muse-browser-'+spec['session'])
        runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
        base=Path(spec['base']);base.mkdir(mode=0o700,parents=True,exist_ok=True)
        cwd=runtime/'cli';cwd.mkdir(mode=0o700,exist_ok=True)
        return spec,runtime,base,cwd

    @contextlib.contextmanager
    def exclusive(self,wid,wait=True):
        lock=self.locks[wid]
        if not lock.acquire(timeout=5 if wait else 0):raise Conflict('Workspace request in progress')
        _,runtime,_,_=self.paths(wid)
        try:
            with (runtime/'control.lock').open('a') as f:
                deadline=time.monotonic()+5
                while True:
                    try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if not wait or time.monotonic()>=deadline:raise Conflict('Existing Muse browser request in progress')
                        time.sleep(.025)
                yield
        finally:lock.release()

    def cli(self,wid,*args,seconds=25,maintenance=False,raw_result=False):
        started=time.monotonic();self.last_timings[wid]={}
        if self.manual_login(wid) and not maintenance:raise BrowserError('Human sign-in is open; browser automation is disconnected')
        spec,runtime,base,cwd=self.paths(wid)
        env={k:v for k,v in os.environ.items() if k in ('HOME','DISPLAY','XAUTHORITY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','XDG_SESSION_TYPE','LANG')}
        env.update(PATH=self.config['nodeBin']+':/usr/bin:/bin',XDG_CACHE_HOME=str(base/'cache'))
        cmd=[self.config['nodeBin']+'/playwright-cli','-s='+spec['session'],*args]
        from .daemon import execute,DispatchUncertain
        if args[0] not in ('open','close'):
            try:fast=execute(base,spec['session'],cwd,args,seconds)
            except DispatchUncertain:raise Uncertain('Daemon deadline/disconnect; inspect outcome before retrying') from None
            if fast is not None:
                self.last_timings[wid]={**getattr(fast,'muse_timing',{}),'guestAdapterMs':(time.monotonic()-started)*1000}
                if raw_result:return fast
                if fast.returncode or '### Error' in fast.stdout:
                    if 'MUSE_STALE_PAGE' in fast.stdout:raise Conflict('Page/document changed; refresh exact identity')
                    raise BrowserError('Browser action failed; inspect the current page before retrying')
                return fast.stdout
        with subprocess.Popen(cmd,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',start_new_session=True) as p:
            try:out,_=p.communicate(timeout=seconds)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(p.pid,signal.SIGKILL);p.communicate()
                raise Uncertain('CLI deadline exceeded; inspect outcome before retrying')
        self.last_timings[wid]={'guestAdapterMs':(time.monotonic()-started)*1000}
        if raw_result:return subprocess.CompletedProcess(cmd,p.returncode or (1 if '### Error' in out else 0),out)
        if p.returncode or '### Error' in out:
            if 'MUSE_STALE_PAGE' in out:raise Conflict('Page/document changed; refresh exact identity')
            # Never expose a CLI error containing user text/URL or generated code.
            raise BrowserError('Browser action failed; inspect the current page before retrying')
        return out

    def human_mode(self,wid,human,locked=False):
        if self.manual_login(wid):
            if not human:raise BrowserError('Finish human sign-in first')
            return {'mode':'human','ordinaryBrowser':True}
        from .desktop_mode import apply_mode
        def change():
            result=apply_mode(lambda *args:self.cli(wid,*args,raw_result=True),human)
            if result.returncode==124:raise Uncertain('Handoff outcome uncertain; keep workspace paused')
            if result.returncode:raise BrowserError('Native file-picker handoff failed; workspace stays paused')
            self.input_modes[wid]=bool(human)
            self.mode_sessions[wid]=self._mode_signature(wid)
            return json.loads(result.stdout.split('### Result\n',1)[1])
        if locked:return change()
        with self.exclusive(wid):return change()

    def _mode_signature(self,wid):
        from .daemon import session
        spec,_,base,_=self.paths(wid);data=session(base,spec['session'])
        if data is None:return None
        try:
            path=Path(data['socketPath']);st=path.stat()
            return (str(path),st.st_ino,st.st_ctime_ns)
        except OSError:return None

    def input_mode_matches(self,wid,human):
        signature=self._mode_signature(wid)
        return signature is not None and self.input_modes.get(wid)==human and self.mode_sessions.get(wid)==signature

    def run(self,wid,request):
        code=self.source.replace('__MUSE_REQUEST__',json.dumps(request,ensure_ascii=True))
        out=self.cli(wid,'run-code',code)
        raw=out.split('### Result\n',1)[-1]
        try:result,_=json.JSONDecoder().raw_decode(raw.strip())
        except ValueError:raise BrowserError('Controller returned an invalid bounded result')
        timing=result.pop('__museTiming',{})
        if request.get('kind') not in ('describe','frame') and self.config.get('state'):
            from .diagnostics import Diagnostics
            Diagnostics(Path(self.config['state'])/'diagnostics').emit('operation','browser',workspace=wid,command=request.get('kind'),outcome='ok',**self.last_timings.get(wid,{}),**timing)
        return result

    def describe(self,wid,force=False,locked=False):
        cached=self.cache.get(wid)
        if cached and not force and time.monotonic()-cached['at']<5:return cached
        if locked:result=self.run(wid,{'kind':'describe'})
        else:
            with self.exclusive(wid):result=self.run(wid,{'kind':'describe'})
        cached={'at':time.monotonic(),'utc':time.time(),**result};self.cache[wid]=cached
        return cached

    def exact(self,wid,page,document,locked=False):
        observed=self.describe(wid,locked=locked)
        if not any(p['id']==page and p['generation']==document for p in observed['pages']):raise Conflict('Exact page/document no longer observed')

    def frame(self,wid,page,document):
        key=(wid,page,document);cached=self.frames.get(key)
        if cached and time.monotonic()-cached['at']<1:return cached['data']
        if self.legacy_waiting(wid):raise Conflict('Browser worker request queued; keep the previous viewer frame')
        with self.exclusive(wid,wait=True):
            if self.legacy_waiting(wid):raise Conflict('Browser worker request queued; keep the previous viewer frame')
            self.exact(wid,page,document,locked=True)
            _,runtime,_,_=self.paths(wid)
            with tempfile.TemporaryDirectory(dir=runtime,prefix='monitor-frame-') as temp:
                p=Path(temp)/'frame.png'
                self.run(wid,{'kind':'frame','page':page,'document':document,'path':str(p)})
                data=p.read_bytes()
        if len(data)>2*1024**2 or not data.startswith(b'\x89PNG\r\n\x1a\n'):raise BrowserError('Frame exceeds bounded PNG format')
        width=int.from_bytes(data[16:20],'big');height=int.from_bytes(data[20:24],'big')
        if not 0<width<=4096 or not 0<height<=4096:raise BrowserError('Frame dimensions exceed bound')
        self.frames[key]={'at':time.monotonic(),'data':data,'width':width,'height':height}
        return data

    def action(self,wid,request,locked=False):
        if locked:result=self.run(wid,request)
        else:
            with self.exclusive(wid):result=self.run(wid,request)
        self.cache.pop(wid,None)
        return result

    def legacy_owned(self,wid):
        _,runtime,_,_=self.paths(wid)
        try:
            data=json.loads((runtime/'lease').read_text())
            return data.get('until',0)>time.monotonic()
        except (OSError,ValueError):return False

    def legacy_waiting(self,wid):
        _,runtime,_,_=self.paths(wid)
        for marker in (runtime/'requests-waiting').glob('*.json'):
            try:
                data=json.loads(marker.read_text())
                if 0<=time.monotonic()-data['started']<12:return True
            except (OSError,ValueError,KeyError,TypeError):pass
        return False

    def marker(self,wid,state):
        _,runtime,base,_=self.paths(wid)
        paused=base/'paused'
        if state['paused']:paused.touch()
        owner=runtime/'monitor-owner.json'
        if state['lease_until'] and state['lease_until']>time.time():
            temp=owner.with_suffix('.new');temp.write_text(json.dumps({'until':state['lease_until'],'generation':state['generation']}));temp.replace(owner)
        else:owner.unlink(missing_ok=True)

    def start_additional(self,wid):
        spec,runtime,base,_=self.paths(wid)
        if self.manual_login(wid):return
        if spec.get('preserveExisting'):return
        profile=Path(spec['profile']);profile.mkdir(mode=0o700,parents=True,exist_ok=True)
        conf=runtime/'config.json'
        conf.write_text(json.dumps({'browser':{'launchOptions':{'executablePath':'/opt/google/chrome/chrome','chromiumSandbox':True,'ignoreDefaultArgs':['--password-store=basic','--use-mock-keychain','--disable-blink-features=AutomationControlled','--enable-unsafe-swiftshader','--unsafely-disable-devtools-self-xss-warnings'],'args':['--disable-sync','--enable-automation','--class=Muse-'+spec['name']]}},'outputMode':'stdout','timeouts':{'action':10000,'navigation':20000}}))
        with self.exclusive(wid):
            listing=self.cli(wid,'list')
            if re.search(rf'(?m)^- {re.escape(spec["session"])}:\s*\n  - status: open$',listing):return
            self.cli(wid,'open','about:blank','--headed','--profile='+str(profile),'--config='+str(conf),seconds=45)

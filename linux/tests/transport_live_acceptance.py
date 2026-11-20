"""Root-only isolated HTTP CONNECT + SSH mux test; no production key changes."""
import base64,concurrent.futures,hashlib,importlib.machinery,importlib.util,json,os,select,shlex,socket,socketserver,subprocess,tempfile,threading,time
from pathlib import Path

if os.geteuid()!=0:raise SystemExit('Run from the authenticated maintenance session.')
source=Path('/home/muse-operator/Documents/muse-monitor/client-tools/guest.sh').read_text()
report={'checks':{}};daemon=None;t=None


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
class Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        remote=None
        try:
            data=bytearray();self.request.settimeout(5)
            while b'\r\n\r\n' not in data:
                part=self.request.recv(1)
                if not part:return
                data.extend(part)
                if len(data)>16384:return
            expected=base64.b64encode(b'fixture-user:fixture-private-value')
            if (b'CONNECT 127.0.0.1:'+str(self.server.target_port).encode()+b' HTTP/1.1') not in data or b'Proxy-Authorization: Basic '+expected not in data:return
            self.server.connections+=1
            if self.server.deny:self.request.sendall(b'HTTP/1.1 407 denied\r\n\r\n');return
            remote=socket.create_connection(('127.0.0.1',self.server.target_port),timeout=5)
            self.request.sendall(b'HTTP/1.1 200 connected\r\n\r\n');self.request.settimeout(None);remote.settimeout(None)
            while True:
                ready,_,_=select.select([self.request,remote],[],[],5)
                for a in ready:
                    b=a.recv(65536)
                    if not b:return
                    (remote if a is self.request else self.request).sendall(b)
        except OSError:pass
        finally:
            if remote:remote.close()


with tempfile.TemporaryDirectory(prefix='muse-transport-test-',dir='/run') as tmp:
    root=Path(tmp);root.chmod(0o755)
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    key=root/'key';subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
    auth=root/'authorized_keys';auth.write_text('restrict '+key.with_suffix('.pub').read_text());auth.chmod(0o644)
    log=root/'commands.jsonl';log.touch();log.chmod(0o600);os.chown(log,1000,1000)
    forced=root/'forced.py';forced.write_text('import os,shlex,json,time\na=shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND",""))\nwith open('+repr(str(log))+',"a") as f:f.write(json.dumps(a)+"\\n")\nif a and a[0]=="wait-once":time.sleep(3)\nprint(json.dumps(a))\n');forced.chmod(0o644)
    config=root/'sshd_config';config.write_text('Port '+str(port)+'\nListenAddress 127.0.0.1\nHostKey /etc/ssh/ssh_host_ed25519_key\nAuthorizedKeysFile '+str(auth)+'\nAllowUsers muse-operator@127.0.0.1\nAuthenticationMethods publickey\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nUsePAM yes\nDisableForwarding yes\nPermitTTY no\nPermitUserRC no\nForceCommand /usr/bin/python3 '+str(forced)+'\n')
    proxy=Proxy(('127.0.0.1',0),Tunnel);proxy.target_port=port;proxy.connections=0;proxy.deny=False
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    changes={"HOST = '192.0.2.22'":"HOST = '127.0.0.1'",'PORT = 2222':'PORT = '+str(port),'PROXY_PORT = 3130':'PROXY_PORT = '+str(proxy.server_address[1]),"USER = 'muse-main'":"USER = 'muse-operator'","KEY = Path.home()/'.ssh/id_ed25519_muse_workspace'":'KEY = Path('+repr(str(key))+')',"STATE = Path.home()/'.cache/muse-workspace-ssh'":'STATE = Path('+repr(str(root/'state'))+')'}
    code=source
    for old,new in changes.items():assert code.count(old)==1;code=code.replace(old,new)
    helper=root/'guest.sh';helper.write_text(code);helper.chmod(0o755)
    env=dict(os.environ,HTTPS_PROXY='http://fixture-user:fixture-private-value@127.0.0.1:1234')
    def call(args,expected=0):
        begin=time.monotonic();r=subprocess.run(['/usr/bin/python3',str(helper),*args],env=env,text=True,capture_output=True,timeout=40)
        assert r.returncode==expected,('test helper exit',r.returncode)
        return r,round((time.monotonic()-begin)*1000,2)
    try:
        subprocess.run(['/usr/sbin/sshd','-t','-f',str(config)],check=True)
        daemon=subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):break
            except OSError:time.sleep(.1)
        first,cold=call(['metadata']);assert json.loads(first.stdout)==['metadata']
        args=['literal',''," leading 'quotes' ",'line1\nline2','$(touch '+str(root/'must-not-exist')+')']
        r,warm=call(args);assert json.loads(r.stdout)==args and not (root/'must-not-exist').exists()
        report['checks']['empty_whitespace_quotes_newlines_and_literal_shell_text_exact']=True
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rs=list(pool.map(lambda n:call(['parallel',str(n)])[0],range(3)))
        assert all(json.loads(r.stdout)==['parallel',str(i)] for i,r in enumerate(rs))
        assert proxy.connections==1
        report['checks']['sequential_and_parallel_calls_reuse_one_connect_and_authentication']=True
        found=False
        for p in Path('/proc').iterdir():
            try:
                a=p.joinpath('cmdline').read_bytes()
                if str(helper).encode() in a and b'--proxy' in a:
                    found=True;assert b'fixture-private-value' not in a and b'fixture-user' not in a
            except (OSError,ValueError):pass
        assert found;report['checks']['proxy_credentials_not_in_process_arguments']=True
        loader=importlib.machinery.SourceFileLoader('fixture_transport',str(helper));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
        t=m.Transport();real_ensure=t.ensure_master
        before=proxy.connections
        t.ensure_master=lambda:t.stop()
        assert t.command(['must-not-dispatch'])==255
        assert proxy.connections==before and not any('must-not-dispatch' in json.loads(x) for x in log.read_text().splitlines())
        report['checks']['master_loss_cannot_fall_back_or_dispatch_command']=True
        t.ensure_master=real_ensure
        with __import__('unittest.mock',fromlist=['patch']).patch.dict(os.environ,env):
            assert t.command(['wait-once'],timeout=.2)==124
        time.sleep(.15)
        assert sum(json.loads(x)==['wait-once'] for x in log.read_text().splitlines())==1
        report['checks']['timed_out_command_recorded_once_never_replayed']=True
        t.stop();proxy.deny=True
        before_count=len(log.read_text().splitlines());begin=time.monotonic()
        call(['denied-before-dispatch'],expected=255)
        assert len(log.read_text().splitlines())==before_count and time.monotonic()-begin<10
        report['checks']['proxy_denial_bounded_and_no_remote_dispatch']=True
        report['timingsMs']={'coldConnectionAndCommand':cold,'warmReusedCommand':warm,'scope':'local fixture only; cloud gateway not measured'}
        report['complete']=True
    except Exception as e:report.update(complete=False,errorType=type(e).__name__)
    finally:
        if t:
            try:t.stop()
            except Exception:pass
        else:
            try:call(['--stop-transport'])
            except Exception:pass
        proxy.shutdown();proxy.server_close()
        if daemon:daemon.terminate();daemon.wait(timeout=10)
        out=Path('/home/muse-operator/Documents/muse-monitor/evidence/transport-live.json');out.write_text(json.dumps(report,indent=2)+'\n');out.chmod(0o644);os.chown(out,1000,1000)
        print(json.dumps(report),flush=True)
        if not report.get('complete'):raise SystemExit(1)

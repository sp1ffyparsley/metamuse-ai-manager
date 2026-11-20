#!/usr/bin/env python3
"""Main-only browser transport; no credentials stored and no command replay."""
import base64,errno,fcntl,hashlib,json,os,select,shlex,socket,ssl,stat,subprocess,sys,time
from pathlib import Path
from urllib.parse import urlsplit

HOST = '192.0.2.22'
PORT = 2222
PROXY_PORT = 3130
USER = 'muse-main'
KEY = Path.home()/'.ssh/id_ed25519_muse_workspace'
STATE = Path.home()/'.cache/muse-workspace-ssh'
PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f'
FINGERPRINT = 'SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA'
ALIAS = 'muse-workspace-pinned'
IDLE_SECONDS = 120


def fingerprint(public_key):
    return 'SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(public_key.split()[1],validate=True)).digest()).decode().rstrip('=')


def proxy_endpoint(env):
    p=urlsplit(env.get('HTTPS_PROXY',''))
    if p.scheme not in ('http','https') or not p.hostname:
        raise ValueError('Runtime HTTPS_PROXY is unavailable; no connection opened.')
    # Preserve the original helper's tunnel gateway port, not the normal proxy port.
    userinfo=p.netloc.rsplit('@',1)[0] if '@' in p.netloc else None
    return p.scheme,p.hostname,userinfo


def proxy_stream():
    scheme,host,userinfo=proxy_endpoint(os.environ)
    conn=socket.create_connection((host,PROXY_PORT),timeout=12)
    try:
        if scheme=='https':conn=ssl.create_default_context().wrap_socket(conn,server_hostname=host)
        target=HOST+':'+str(PORT)
        headers=['CONNECT '+target+' HTTP/1.1','Host: '+target]
        if userinfo is not None:
            # Runtime-only proxy auth: never in argv, source, files or logs.
            headers.append('Proxy-Authorization: Basic '+base64.b64encode(userinfo.encode()).decode())
        conn.sendall(('\r\n'.join(headers)+'\r\n\r\n').encode())
        response=bytearray()
        while b'\r\n\r\n' not in response:
            part=conn.recv(1)
            if not part:raise OSError('Proxy closed before CONNECT response')
            response.extend(part)
            if len(response)>16384:raise OSError('Proxy response exceeds header bound')
        words=bytes(response).split(b'\r\n',1)[0].split()
        if len(words)<2 or words[1]!=b'200':raise OSError('Proxy CONNECT denied')
        conn.settimeout(None)
        while True:
            ready,_,_=select.select([conn,0],[],[])
            if conn in ready:
                data=conn.recv(65536)
                if not data:break
                view=memoryview(data)
                while view:view=view[os.write(1,view):]
            if 0 in ready:
                data=os.read(0,65536)
                if not data:break
                conn.sendall(data)
    finally:conn.close()


class Transport:
    def __init__(self):
        os.umask(0o077)
        if fingerprint(PUBLIC_KEY)!=FINGERPRINT:raise ValueError('Pinned public host key does not match fingerprint')
        STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
        if STATE.is_symlink() or STATE.stat().st_uid!=os.getuid() or STATE.stat().st_mode&0o077:
            raise ValueError('Transport state must be a private directory owned by this account')
        self.control=STATE/'control'
        if len(str(self.control).encode())>100:raise ValueError('Transport state path exceeds Unix socket bound')
        self.known=STATE/'known_hosts'
        record=ALIAS+' '+PUBLIC_KEY+'\n'
        if self.known.exists() or self.known.is_symlink():
            if self.known.is_symlink() or self.known.stat().st_uid!=os.getuid() or self.known.stat().st_mode&0o077 or self.known.read_text()!=record:
                raise ValueError('Pinned known_hosts changed; do not overwrite it automatically')
        else:
            fd=os.open(self.known,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(record)
        if not KEY.is_file():raise ValueError('Existing dedicated workspace key is missing; do not generate a replacement automatically')

    def base(self):
        return ['ssh','-F','/dev/null','-n','-T','-i',str(KEY),'-p',str(PORT),
                '-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
                '-o','HostKeyAlias='+ALIAS,'-o','HostKeyAlgorithms=ssh-ed25519',
                '-o','UserKnownHostsFile='+str(self.known),'-o','GlobalKnownHostsFile=/dev/null',
                '-o','ClearAllForwardings=yes','-o','ForwardAgent=no','-o','ForwardX11=no',
                '-o','ConnectTimeout=12','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',
                '-S',str(self.control)]

    def check(self):
        return subprocess.run(self.base()+['-o','ProxyCommand=false','-O','check',USER+'@'+HOST],
                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=4).returncode==0

    def ensure_master(self):
        fd=os.open(STATE/'setup.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as gate:
            deadline=time.monotonic()+25
            while True:
                try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise TimeoutError('Transport setup is already in progress')
                    time.sleep(.025)
            if self.check():return
            if self.control.exists():
                info=self.control.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                    raise ValueError('Unexpected transport control path; nothing removed')
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(.2)
                    code=probe.connect_ex(str(self.control))
                if code not in (errno.ECONNREFUSED,errno.ENOENT):
                    raise ValueError('Existing transport is not responding; no browser command sent')
                self.control.unlink(missing_ok=True)
            # Only connection establishment may be retried by a later invocation.
            # This master opens no session and sends no browser command.
            proxy=shlex.join([sys.executable,str(Path(__file__).resolve()),'--proxy'])
            code=subprocess.run(self.base()+['-o','ControlMaster=yes','-o','ControlPersist='+str(IDLE_SECONDS),
                                            '-o','ProxyCommand='+proxy,'-N','-f',USER+'@'+HOST],
                                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25).returncode
            if code or not self.check():raise OSError('Pinned SSH/proxy setup failed before any browser command was sent')

    def command(self,args,timeout=70):
        self.ensure_master()
        # A command must use the existing master. If it vanishes, false prevents
        # SSH from falling back to a fresh connection and replaying the command.
        argv=self.base()+['-o','ControlMaster=no','-o','ProxyCommand=false',USER+'@'+HOST,shlex.join(args)]
        proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL)
        try:code=proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:proc.wait(timeout=2)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            code=124
        except OSError:
            # Dispatch already began; never describe a later wait failure as
            # proof that no browser command was sent.
            code=255
        if code in (124,255):
            print('MUSE_TRANSPORT_OUTCOME_UNCERTAIN: inspect browser/receipt before retrying; no automatic command replay.',file=sys.stderr)
        return code

    def stop(self):
        return subprocess.run(self.base()+['-o','ProxyCommand=false','-O','exit',USER+'@'+HOST],
                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode


def main(args):
    if args==['--proxy']:
        try:proxy_stream();return 0
        except (OSError,ValueError):
            print('MUSE_PROXY_UNAVAILABLE: runtime proxy connection failed; credentials omitted.',file=sys.stderr);return 255
    t=Transport()
    if args==['--transport-status']:
        print(json.dumps({'connected':t.check(),'host':HOST,'port':PORT,'fingerprint':FINGERPRINT,'idleSeconds':IDLE_SECONDS}));return 0
    if args==['--stop-transport']:return t.stop()
    if not args:raise ValueError('Use guest.sh HANDLER_COMMAND [ARGS]; browser credentials remain unchanged')
    return t.command(args)


if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except (OSError,ValueError,subprocess.TimeoutExpired):
        print('MUSE_TRANSPORT_SETUP_FAILED: no new browser command sent; check existing key, pinned host and runtime proxy.',file=sys.stderr)
        raise SystemExit(255)

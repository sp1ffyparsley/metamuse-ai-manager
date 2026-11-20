"""Install or roll back only MetaMuse's controller code and scoped worker."""
import argparse
import fcntl
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from hashlib import sha256

SERVICE = 'muse-monitor.service'
INSTALL = Path('/opt/muse-monitor')
PYTHON = '/opt/muse-monitor-venv/bin/python'
BACKUPS = Path('/var/backups/muse-monitor/metamuse')
IPC = Path('/var/lib/muse-monitor-manager')
WORKER = 'manager-hermes'


def run(*args, timeout=35):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=timeout).stdout.strip()


def atomic(path, content, uid, gid, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.metamuse-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def private_directory(path, user):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid not in (0, user.pw_uid):
        raise RuntimeError(f'Unsafe private configuration directory: {path}')
    os.chown(path, user.pw_uid, user.pw_gid)
    path.chmod(0o700)


def check_idle(muse, details):
    if run('systemctl', 'show', '-p', 'KillMode', '--value', SERVICE) != 'process':
        raise RuntimeError('Existing controller KillMode must be process to preserve browser owners')
    code = '''
import json, sys
from pathlib import Path
from muse_monitor.cli import UnixHTTP
config = json.loads(Path(sys.argv[1]).read_text())
token = Path(config['principalFiles']['local-human']).read_text().strip()
conn = UnixHTTP(str(Path(config['runtime']) / 'muse-monitor.sock'))
def read(route):
    conn.request('GET', route, headers={'Authorization':'Bearer '+token,'X-Muse-Protocol':'1'})
    response=conn.getresponse()
    result=json.loads(response.read(1048576))
    if response.status != 200: raise RuntimeError('Controller probe denied')
    return result
status=read('/v1/status')
health=read('/v1/health')
if sys.argv[2] == 'connections':
    result=read('/v1/connections')
    print(json.dumps({'revision':status['revision'], 'entries':len(result['entries'])}))
elif sys.argv[2] == 'google':
    conn.request('POST','/v1/connections/verify',json.dumps({'id':'google'}),
                 {'Authorization':'Bearer '+token,'X-Muse-Protocol':'1','Content-Type':'application/json'})
    response=conn.getresponse()
    result=json.loads(response.read(1048576))
    if response.status != 200: raise RuntimeError('Connection check denied')
    google=next(entry for entry in result['entries'] if entry['id']=='google')
    print(json.dumps({'id':'google','status':google['status'],'detail':google['detail']}))
else:
    rows=[{'id':row['id'],'state':row['state'],'owned':bool(row['actor'])} for row in status['workspaces']]
    print(json.dumps({'rows':rows,'uncertain':len(health['uncertain']),'pendingSync':health['pendingSync']}))
'''
    result = json.loads(run('runuser', '-u', muse.pw_name, '--', 'env', 'PYTHONPATH=' + str(INSTALL),
                            PYTHON, '-c', code, str(Path(muse.pw_dir) / '.config/muse-monitor/config.json'),
                            details, timeout=50))
    if details == 'idle' and (not result['rows'] or result['uncertain'] or result['pendingSync'] or
            any(row['owned'] or row['state'] not in ('waiting', 'paused') for row in result['rows'])):
        raise RuntimeError('Occupied browser, pending sync, or uncertain action. Existing controller was left running.')
    return result


def browser_owners(muse):
    number = str(muse.pw_uid)
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if str(entry.stat().st_uid) != number:
                continue
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            program = argv[0].rsplit(b'/', 1)[-1]
            if program in (b'chrome', b'chromium', b'google-chrome') and not any(arg.startswith(b'--type=') for arg in argv):
                result[entry.name] = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError):
            continue
    return result


def systemd_user(user, *args):
    runtime = '/run/user/' + str(user.pw_uid)
    return run('runuser', '-u', user.pw_name, '--', 'env', 'XDG_RUNTIME_DIR=' + runtime,
               'DBUS_SESSION_BUS_ADDRESS=unix:path=' + runtime + '/bus', 'systemctl', '--user', *args)


def worker_units(user, source):
    root = Path(user.pw_dir) / '.config'
    for component in (root, root / 'systemd', root / 'systemd/user'):
        if not component.exists():
            component.mkdir(mode=0o700)
            os.chown(component, user.pw_uid, user.pw_gid)
        if component.is_symlink() or not component.is_dir() or component.stat().st_uid != user.pw_uid:
            raise RuntimeError(f'Unsafe user unit location: {component}')
    for name in ('metamuse-worker.path', 'metamuse-worker.service'):
        file = root / 'systemd/user' / name
        atomic(file, (source / 'systemd' / name).read_text(), user.pw_uid, user.pw_gid, 0o644)
    systemd_user(user, 'daemon-reload')
    systemd_user(user, 'enable', '--now', 'metamuse-worker.path')


def wait_for(muse, check):
    started = time.monotonic()
    for _ in range(18):
        try:
            result = check_idle(muse, check)
            return result, round((time.monotonic() - started) * 1000)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
            time.sleep(1)
    raise RuntimeError('Controller did not pass post-install verification. Inspect the existing service.')


def upgrade(source, rev, muse, user, path):
    config = json.loads(path.read_text())
    if config.get('google', {}).get('productionEnabled'):
        raise RuntimeError('Canonical writer is enabled; coordinate the current writer before deploying this build')
    current = config.get('revision', '')
    wanted = rev + '+metamuse'
    private = Path(user.pw_dir) / '.config/metamuse'
    hermes = Path(user.pw_dir) / '.local/share/metamuse-hermes'
    if not (hermes / 'venv/bin/hermes').is_file():
        raise RuntimeError('Pinned user-private Hermes runtime is not installed')
    before = check_idle(muse, 'idle')
    owners = browser_owners(muse)
    if not owners:
        raise RuntimeError('Existing browser owner processes could not be identified')
    print('[MetaMuse] preflight', json.dumps(before), 'browserOwners=' + str(len(owners)), flush=True)
    if current == wanted:
        worker_units(user, source)
        print('[MetaMuse] already installed', json.dumps(check_idle(muse, 'connections')), flush=True)
        return
    config['manager'] = {**config.get('manager', {}), 'workerSocket': str(IPC / 'worker.sock'), 'wakePath': str(IPC / 'ready')}
    expected_account = os.environ.get('METAMUSE_FOOT_ACCOUNT')
    document = os.environ.get('METAMUSE_GOOGLE_DOC')
    if expected_account and document and re.fullmatch(r'[A-Za-z0-9_-]{15,150}', document):
        config['manager']['google'] = {'account': expected_account, 'docsProbeId': document}
    config['revision'] = wanted
    private_directory(private, user)
    token_path = private / 'worker.key'
    if token_path.is_symlink():
        raise RuntimeError('Existing worker credential path is not a regular private file')
    if token_path.exists():
        info = token_path.stat()
        if not token_path.is_file() or info.st_uid != user.pw_uid or info.st_mode & 0o077 or info.st_size > 128:
            raise RuntimeError('Existing worker credential ownership or mode is unsafe')
        token = token_path.read_text().strip()
    else:
        token = secrets.token_urlsafe(42)
        atomic(token_path, token + '\n', user.pw_uid, user.pw_gid)
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', token):
        raise RuntimeError('Existing worker credential has invalid format')
    config['principals'][WORKER] = {'role': 'manager-worker', 'workspaces': [],
                                    'hash': sha256(token.encode()).hexdigest()}
    atomic(private / 'worker.json', json.dumps({'socket': config['manager']['workerSocket'],
           'credentialFile': str(token_path), 'hermesRoot': str(hermes)}) + '\n', user.pw_uid, user.pw_gid)
    IPC.mkdir(mode=0o750, exist_ok=True)
    if IPC.is_symlink() or not IPC.is_dir():
        raise RuntimeError('Unsafe manager Unix socket directory')
    os.chown(IPC, muse.pw_uid, user.pw_gid)
    IPC.chmod(0o2750)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = BACKUPS / ('previous-' + str(int(time.time())))
    backup.mkdir(mode=0o700)
    shutil.copy2(path, backup / 'config.json')
    shutil.copytree(INSTALL / 'muse_monitor', backup / 'muse_monitor')
    with tempfile.TemporaryDirectory(dir=INSTALL, prefix='.metamuse-stage-') as directory:
        stage = Path(directory)
        shutil.copytree(source / 'muse_monitor', stage / 'package', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        run(PYTHON, '-m', 'compileall', '-q', str(stage / 'package'))
        shutil.rmtree(stage / 'package/__pycache__')
        check_idle(muse, 'idle')
        try:
            (INSTALL / 'muse_monitor').rename(stage / 'old')
            (stage / 'package').rename(INSTALL / 'muse_monitor')
            atomic(path, json.dumps(config, indent=2) + '\n', muse.pw_uid, muse.pw_gid)
            run('systemctl', 'restart', SERVICE)
            result, elapsed = wait_for(muse, 'connections')
            if result['revision'] != wanted or not owners.items() <= browser_owners(muse).items():
                raise RuntimeError('Installed revision or original browser owner process changed')
        except Exception:
            restore(backup, path, muse)
            raise
    worker_units(user, source)
    print(f'[MetaMuse] installed revision={wanted} reconnect_ms={elapsed} backup={backup}', flush=True)
    print('[MetaMuse] live Google probe', json.dumps(check_idle(muse, 'google')), flush=True)


def restore(backup, config, muse):
    saved = json.loads((backup / 'config.json').read_text())
    current = json.loads(config.read_text())
    current['revision'] = saved['revision']
    if 'manager' in saved:
        current['manager'] = saved['manager']
    else:
        current.pop('manager', None)
    if WORKER in saved['principals']:
        current['principals'][WORKER] = saved['principals'][WORKER]
    else:
        current['principals'].pop(WORKER, None)
    with tempfile.TemporaryDirectory(dir=INSTALL, prefix='.metamuse-rollback-') as directory:
        work = Path(directory)
        shutil.copytree(backup / 'muse_monitor', work / 'package')
        if (INSTALL / 'muse_monitor').exists():
            (INSTALL / 'muse_monitor').rename(work / 'retired')
        (work / 'package').rename(INSTALL / 'muse_monitor')
        atomic(config, json.dumps(current, indent=2) + '\n', muse.pw_uid, muse.pw_gid)
        run('systemctl', 'restart', SERVICE)
        wait_for(muse, 'idle')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--manager-upgrade', action='store_true')
    action.add_argument('--manager-rollback', metavar='BACKUP')
    parser.add_argument('--expected')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run from the authenticated Ubuntu maintenance desktop with sudo')
    os.umask(0o077)
    user = pwd.getpwnam('muse-operator')
    muse = pwd.getpwnam('muse-main')
    source = Path(__file__).resolve().parent
    config = Path(muse.pw_dir) / '.config/muse-monitor/config.json'
    with Path('/run/lock/metamuse-deployment.lock').open('a') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.manager_upgrade:
            git = ('runuser', '-u', user.pw_name, '--', 'git', '-C', str(source.parent))
            revision = run(*git, 'rev-parse', 'HEAD')
            if args.expected != revision or run(*git, 'status', '--porcelain', '--untracked-files=no'):
                parser.error('Expected exact clean source revision; no deployment was made')
            print('[MetaMuse] loading installed configuration from', config, flush=True)
            upgrade(source, revision, muse, user, config)
        else:
            if not re.fullmatch(r'previous-\d{10}', args.manager_rollback):
                parser.error('Supply the recorded backup directory name')
            backup = BACKUPS / args.manager_rollback
            if not (backup / 'config.json').is_file():
                parser.error('Verified prior configuration backup is missing')
            check_idle(muse, 'idle')
            systemd_user(user, 'disable', '--now', 'metamuse-worker.path')
            if systemd_user(user, 'show', 'metamuse-worker.service', '--property=ActiveState', '--value') in ('active', 'activating'):
                raise RuntimeError('Wait for the current scoped inference before rolling back')
            restore(backup, config, muse)
            print('[MetaMuse] restored prior controller revision from', backup, flush=True)

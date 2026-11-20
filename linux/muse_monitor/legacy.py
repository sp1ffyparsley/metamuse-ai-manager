"""Optional guards for the unchanged legacy browser protocol."""
import contextlib,fcntl,json,time
from pathlib import Path

@contextlib.contextmanager
def admission(runtime):
    """Only claim admission is global; browser actions stay per workspace."""
    with (runtime.parent/'muse-browser-admission.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        yield

def guard(runtime):
    try:owner=json.loads((runtime/'monitor-owner.json').read_text())
    except (OSError,ValueError):return
    if owner.get('until',0)>time.time():raise ValueError('Muse Monitor owns this workspace. Use its existing lease or wait for release.')

def legacy_capacity(runtime):
    config=Path.home()/'.config/muse-monitor/config.json'
    if not config.exists():return
    data=json.loads(config.read_text());active=0
    for spec in data['workspaces'].values():
        folder=runtime.parent/('muse-browser-'+spec['session'])
        try:
            owner=json.loads((folder/'monitor-owner.json').read_text());active+=owner.get('until',0)>time.time()
        except (OSError,ValueError):pass
    if active>=data.get('capacity',2):raise ValueError('Browser worker capacity is occupied; continue preparation without a browser lease.')

def local_resume():
    config=Path.home()/'.config/muse-monitor/config.json'
    if not config.exists():return
    from .cli import request
    request(config,{'op':'local-resume','workspace':'jobs'},principal='local-human')

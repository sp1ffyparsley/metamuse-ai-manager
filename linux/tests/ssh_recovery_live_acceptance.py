"""Root-only isolated systemd dependency/restart test; no live network restart."""
import json,os,subprocess,time,uuid
from pathlib import Path

if os.geteuid()!=0:raise SystemExit('Use the authenticated maintenance session.')
tag='muse-recovery-probe-'+uuid.uuid4().hex
up=tag+'-up.service';down=tag+'-down.service';folder=Path('/run/systemd/system')
wants=folder/(up+'.wants')
report={'checks':{},'restartSeconds':5,'crashCycles':6}
def ctl(*args,check=True):
    return subprocess.run(['systemctl',*args],check=check,capture_output=True,text=True,timeout=15)
def prop(unit,name):return ctl('show',unit,'-p',name,'--value').stdout.strip()
def wait(predicate):
    end=time.monotonic()+12
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.15)
    raise AssertionError('Isolated recovery deadline exceeded')

before={u:prop(u,'MainPID') for u in ('tailscaled.service','muse-ssh.service','muse-monitor.service')}
try:
    (folder/up).write_text('[Unit]\nDescription=Disposable Muse recovery upstream fixture\nStartLimitIntervalSec=10s\nStartLimitBurst=5\n[Service]\nExecStart=/usr/bin/sleep infinity\nRestart=on-failure\nRestartSec=5s\n')
    (folder/down).write_text('[Unit]\nDescription=Disposable Muse recovery dependent fixture\nRequires='+up+'\nAfter='+up+'\nPartOf='+up+'\n[Service]\nExecStart=/usr/bin/sleep infinity\nRestart=on-failure\n')
    wants.mkdir();(wants/down).symlink_to(folder/down)
    ctl('daemon-reload');ctl('start',up)
    wait(lambda:prop(down,'ActiveState')=='active')
    assert prop(up,'RestartUSec')=='5s'
    for _ in range(report['crashCycles']):
        oldup=prop(up,'MainPID');olddown=prop(down,'MainPID')
        ctl('kill','--kill-whom=main','--signal=KILL',up)
        wait(lambda:prop(up,'ActiveState')=='active' and prop(up,'MainPID') not in ('0',oldup) and prop(down,'ActiveState')=='active' and prop(down,'MainPID') not in ('0',olddown))
    report['checks']['dependent_returns_after_upstream_crash_and_automatic_restart']=True
    report['checks']['six_crashes_do_not_exhaust_native_start_limit']=True
    ctl('stop',up);wait(lambda:prop(down,'ActiveState')=='inactive')
    ctl('start',up);wait(lambda:prop(down,'ActiveState')=='active')
    report['checks']['dependent_returns_after_explicit_stop_start']=True
    report['complete']=True
except Exception as e:report.update(complete=False,errorType=type(e).__name__)
finally:
    ctl('stop',up,down,check=False)
    (wants/down).unlink(missing_ok=True)
    if wants.exists():wants.rmdir()
    for name in (up,down):(folder/name).unlink(missing_ok=True)
    ctl('daemon-reload');ctl('reset-failed',up,down,check=False)
    report['checks']['live_tailscale_ssh_controller_pids_unchanged']=before=={u:prop(u,'MainPID') for u in before}
    report['checks']['temporary_units_removed']=not any((folder/n).exists() for n in (up,down))
    report['complete']=report.get('complete',False) and all(report['checks'].values())
    target=Path('/home/muse-operator/Documents/muse-monitor/evidence/ssh-recovery-overnight-20261115.json')
    target.write_text(json.dumps(report,indent=2)+'\n');target.chmod(0o644);os.chown(target,1000,1000)
    print(json.dumps(report),flush=True)
    if not report['complete']:raise SystemExit(1)

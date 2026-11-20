#!/usr/bin/python3
"""Install the controller extension on the existing VM; never restart Chrome."""
import base64,fcntl,hashlib,json,os,pwd,secrets,shutil,subprocess,sys
from pathlib import Path
if os.geteuid()!=0:raise SystemExit('Run from the authenticated maintenance session with sudo.')
if any(arg in sys.argv for arg in ('--manager-upgrade', '--manager-rollback')):
    from manager_deploy import main
    main()
    raise SystemExit(0)
source=Path(__file__).resolve().parent
user=pwd.getpwnam('muse-main');home=Path(user.pw_dir)
base=home/'.config/muse-monitor';base.mkdir(mode=0o700,parents=True,exist_ok=True)
state=home/'.local/share/muse-monitor';state.mkdir(mode=0o700,parents=True,exist_ok=True)
def private(path,text):
    p=Path(path);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    p.write_text(text);p.chmod(0o600);os.chown(p,user.pw_uid,user.pw_gid)
    return p
def run(*cmd):subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL)
installed=Path('/opt/muse-monitor');installed.mkdir(mode=0o755,exist_ok=True)
shutil.copytree(source/'icons',installed/'icons',dirs_exist_ok=True)
shutil.copytree(source/'muse_monitor',installed/'muse_monitor',dirs_exist_ok=True)
shutil.copyfile(source/'canonical-schemas.json',installed/'canonical-schemas.json')
shutil.copyfile(source/'start.py',installed/'start.py')
venv=Path('/opt/muse-monitor-venv')
if not (venv/'bin/python').exists():run('python3','-m','venv',str(venv))
run(str(venv/'bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(source/'requirements.lock'))
ca=Path('/var/lib/muse-monitor-tls');ca.mkdir(mode=0o700,exist_ok=True)
if not (ca/'ca.crt').exists():
    run('openssl','req','-x509','-newkey','rsa:3072','-nodes','-keyout',str(ca/'ca.key'),'-out',str(ca/'ca.crt'),'-days','365','-subj','/CN=Muse Monitor private development CA','-addext','basicConstraints=critical,CA:TRUE,pathlen:0','-addext','keyUsage=critical,keyCertSign,cRLSign')
tls=base/'tls';tls.mkdir(mode=0o700,exist_ok=True)
if not (tls/'server.crt').exists():
    run('openssl','req','-new','-newkey','rsa:3072','-nodes','-keyout',str(tls/'server.key'),'-out',str(ca/'server.csr'),'-subj','/CN=muse-workspace')
    (ca/'server.ext').write_text('subjectAltName=IP:192.0.2.128,DNS:muse-workspace\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
    run('openssl','x509','-req','-in',str(ca/'server.csr'),'-CA',str(ca/'ca.crt'),'-CAkey',str(ca/'ca.key'),'-CAcreateserial','-out',str(tls/'server.crt'),'-days','180','-extfile',str(ca/'server.ext'))
shutil.copyfile(ca/'ca.crt',base/'controller-ca.crt')
config_file=base/'config.json'
if config_file.exists():config=json.loads(config_file.read_text())
else:
    principals={};files={}
    for actor,role,workspaces in [('windows-human','human',['jobs','scholarships','research']),('local-human','human',['jobs']),('main','main',['jobs','scholarships','research']),('extension-jobs','human',['jobs']),('extension-scholarships','human',['scholarships']),('extension-research','human',['research'])]:
        token=secrets.token_urlsafe(48);file=private(base/(actor+'.token'),token+'\n');principals[actor]={'hash':hashlib.sha256(token.encode()).hexdigest(),'role':role,'workspaces':workspaces};files[actor]=str(file)
    config={'bind':'192.0.2.128','port':8443,'runtime':'/run/user/1001','state':str(state),'nodeBin':'/opt/muse/node/bin','capacity':2,'principals':principals,'principalFiles':files,'workspaces':{
        'jobs':{'name':'Jobs','session':'main','base':str(home/'.local/share/muse-browser'),'profile':str(home/'.local/share/muse-browser/profile'),'preserveExisting':True},
        'scholarships':{'name':'Scholarships','session':'scholarships','base':str(home/'.local/share/muse-workspaces/scholarships'),'profile':str(home/'.local/share/muse-workspaces/scholarships/profile')},
        'research':{'name':'Research','session':'research','base':str(home/'.local/share/muse-workspaces/research'),'profile':str(home/'.local/share/muse-workspaces/research/profile')}},'tls':{'certificate':str(tls/'server.crt'),'key':str(tls/'server.key')},'google':{'token':str(base/'google-token.json'),'productionEnabled':False}}
try:revision=subprocess.check_output(['git','-C',str(source.parent),'rev-parse','HEAD'],text=True).strip()
except subprocess.CalledProcessError:revision='uncommitted-development'
config['revision']=revision+'+linux-development'
# The existing local desktop control handles every configured resource. This
# changes no remote worker or SSH authority.
if 'local-human' in config['principals']:config['principals']['local-human']['workspaces']=list(config['workspaces'])
private(config_file,json.dumps(config,indent=2)+'\n')
extension=source.parent/'extension'
if (extension/'manifest.json').exists():
    manifest=json.loads((extension/'manifest.json').read_text());key=base64.b64decode(manifest['key'])
    config['extensionId']=''.join(chr(ord('a')+int(c,16)) for c in hashlib.sha256(key).digest()[:16].hex())
    shutil.copytree(extension,installed/'extension',dirs_exist_ok=True)
    native=installed/'native-host';native.write_text('#!/bin/sh\nexport PYTHONPATH=/opt/muse-monitor\nexec /opt/muse-monitor-venv/bin/python -m muse_monitor.native "$@"\n');native.chmod(0o755)
    native_manifest={'name':'com.muse.monitor','description':'Muse Workspace scoped native bridge','path':str(native),'type':'stdio','allowed_origins':['chrome-extension://'+config['extensionId']+'/']}
    for folder in [home/'.config/google-chrome/NativeMessagingHosts',*[Path(s['profile'])/'NativeMessagingHosts' for s in config['workspaces'].values()]]:
        folder.mkdir(mode=0o700,parents=True,exist_ok=True);private(folder/'com.muse.monitor.json',json.dumps(native_manifest))
        os.chown(folder,user.pw_uid,user.pw_gid)
    private(config_file,json.dumps(config,indent=2)+'\n')
for p in [base,state,*base.rglob('*')]:
    os.chown(p,user.pw_uid,user.pw_gid)
    p.chmod(0o700 if p.is_dir() else 0o600)
human=installed/'human-login'
human.write_text('''#!/bin/sh
export PYTHONPATH=/opt/muse-monitor
result=$(/opt/muse-monitor-venv/bin/python -m muse_monitor.human_login "$@" 2>&1)
code=$?
printf '%s\\n' "$result"
if [ "${MUSE_HUMAN_LOGIN_NOTIFY:-}" = 1 ]; then
 /usr/bin/notify-send --expire-time=15000 'Muse Workspace' "$result"
fi
exit "$code"
''');human.chmod(0o755)
tasks=installed/'task-view'
tasks.write_text('#!/bin/sh\nexport PYTHONPATH=/opt/muse-monitor\nexec /opt/muse-monitor-venv/bin/python -m muse_monitor.task_view "$@"\n');tasks.chmod(0o755)
shutil.copyfile(source.parent/'TASK-WORKFLOW.md',installed/'TASK-WORKFLOW.md')
desktop=home/'Desktop';desktop.mkdir(mode=0o700,exist_ok=True);os.chown(desktop,user.pw_uid,user.pw_gid)
task_shortcut=desktop/'Muse Tasks.desktop'
task_shortcut.write_text('[Desktop Entry]\nType=Application\nName=Muse Tasks\nComment=Ready work, Needs you questions and checked completion receipts\nExec=/opt/muse-monitor/task-view\nIcon=view-list-symbolic\nTerminal=false\n')
task_shortcut.chmod(0o755);os.chown(task_shortcut,user.pw_uid,user.pw_gid)
for wid,spec in config['workspaces'].items():
    name=spec['name'];shortcut=desktop/('Muse '+name+'.desktop')
    icon=str(installed/'icons'/('main.svg' if wid=='jobs' else 'secondary.svg'))
    shortcut.write_text(f'''[Desktop Entry]
Type=Application
Name=Muse {name}
Comment=Open this existing profile; use Sign In for an attended login handoff
Exec=env MUSE_HUMAN_LOGIN_NOTIFY=1 /opt/muse-monitor/human-login {wid} show
Icon={icon}
Terminal=false
StartupWMClass=Muse-{name}
Actions=SignIn;Finish;Status;

[Desktop Action SignIn]
Name=Sign in manually (pause automation)
Exec=env MUSE_HUMAN_LOGIN_NOTIFY=1 /opt/muse-monitor/human-login {wid} begin

[Desktop Action Finish]
Name=Finish human sign-in (close this Chrome window first)
Exec=env MUSE_HUMAN_LOGIN_NOTIFY=1 /opt/muse-monitor/human-login {wid} finish

[Desktop Action Status]
Name=Show workspace login state
Exec=env MUSE_HUMAN_LOGIN_NOTIFY=1 /opt/muse-monitor/human-login {wid} status
''');shortcut.chmod(0o755);os.chown(shortcut,user.pw_uid,user.pw_gid)
    apps=home/'.local/share/applications';apps.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(apps,user.pw_uid,user.pw_gid)
    appfile=apps/('muse-workspace-'+wid+'.desktop');shutil.copyfile(shortcut,appfile);appfile.chmod(0o644);os.chown(appfile,user.pw_uid,user.pw_gid)
public=Path('/home/muse-operator/.cache/muse-monitor-public');public.mkdir(mode=0o700,exist_ok=True)
shutil.copyfile(ca/'ca.crt',public/'controller-ca.crt');os.chown(public,1000,1000);os.chown(public/'controller-ca.crt',1000,1000)
guard=Path('/usr/local/libexec/muse-monitor-firewall')
guard.write_text('''#!/usr/bin/python3
import subprocess
present=subprocess.run(['nft','list','table','inet','muse_monitor_guard'],capture_output=True).returncode==0
rules=('delete table inet muse_monitor_guard\\n' if present else '')+'''+repr('''table inet muse_monitor_guard {
 chain input {
 type filter hook input priority -150; policy accept;
 iifname "ens33" ip saddr 192.0.2.1 ip daddr 192.0.2.128 tcp dport 8443 accept
 tcp dport 8443 reject with tcp reset
 udp dport 8443 drop
 }
}
''')+'''
subprocess.run(['nft','-f','-'],input=rules,text=True,check=True)
''');guard.chmod(0o755)
Path('/etc/systemd/system/muse-monitor-firewall.service').write_text('''[Unit]
Description=Restrict Muse Monitor TLS API to the Windows VMnet8 host
Before=muse-monitor.service
[Service]
Type=oneshot
ExecStart=/usr/local/libexec/muse-monitor-firewall
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''')
Path('/etc/systemd/system/muse-monitor.service').write_text('''[Unit]
Description=Muse Monitor controller extension (existing Muse desktop)
After=network-online.target user@1001.service display-manager.service muse-monitor-firewall.service
Wants=network-online.target
Requires=muse-monitor-firewall.service
[Service]
Type=simple
User=muse-main
Group=muse-main
Environment=HOME=/home/muse-main
Environment=XDG_RUNTIME_DIR=/run/user/1001
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus
Environment=PYTHONPATH=/opt/muse-monitor
ExecStart=/opt/muse-monitor-venv/bin/python /opt/muse-monitor/start.py
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=10
UMask=0077
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
''')
# The browser controller is migrated only after tests explicitly authorize it.
# --install-legacy never changes SSH configuration or its key restrictions.
if '--install-legacy' in sys.argv:
    control=Path('/usr/local/libexec/muse-browser/control');backup=Path('/var/lib/muse-browser/muse-monitor-backup');backup.mkdir(mode=0o700,exist_ok=True)
    if not (backup/'control').exists():shutil.copyfile(control,backup/'control')
    with Path('/run/user/1001/muse-browser-main/control.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        temporary=control.with_suffix('.new');shutil.copyfile(source/'legacy-control.py',temporary);temporary.chmod(0o755);temporary.replace(control)
    # Requires/PartOf stops SSH when Tailscale fails, but does not bring it
    # back after a failed dependency job. Start the unchanged restricted SSH
    # service whenever the existing Tailscale service starts successfully.
    if Path('/etc/systemd/system/muse-ssh.service').exists():
        run('systemctl','add-wants','tailscaled.service','muse-ssh.service')
run('systemctl','daemon-reload')
run('systemctl','enable','--now','muse-monitor-firewall.service','muse-monitor.service')
print('Muse Monitor service installed; Chrome sessions and legacy SSH configuration preserved. Google authorization is separate.')

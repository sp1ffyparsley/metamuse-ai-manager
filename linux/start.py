#!/usr/bin/python3
"""Wait for the existing desktop environment; never log users out or in."""
import os,subprocess,time
for attempt in range(90):
    try:
        r=subprocess.run(['systemctl','--user','show-environment'],capture_output=True,text=True,timeout=3)
        env=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
        if env.get('DISPLAY'):
            for k in ('DISPLAY','XAUTHORITY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','XDG_SESSION_TYPE'):
                if k in env:os.environ[k]=env[k]
            binary='/opt/muse-monitor-venv/bin/python'
            os.execv(binary,[binary,'-m','muse_monitor.server','--config',os.path.expanduser('~/.config/muse-monitor/config.json')])
    except (OSError,subprocess.TimeoutExpired):pass
    time.sleep(1)
raise SystemExit('Muse desktop environment unavailable; existing sessions were not changed.')

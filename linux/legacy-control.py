#!/usr/bin/python3
"""Control one persistent Playwright browser under the current role UID."""
import base64
import contextlib
import fcntl
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

REMOTE_COMMANDS = {
    "goto": (1, 1), "snapshot": (0, 0), "screenshot": (0, 0), "find": (1, 1),
    "click": (1, 1), "dblclick": (1, 1), "hover": (1, 1),
    "fill": (2, 2), "clear": (1, 1), "type": (1, 2), "press": (1, 2), "read-value": (1, 1),
    "check": (1, 1), "uncheck": (1, 1), "select": (2, 2),
    "go-back": (0, 0), "go-forward": (0, 0), "reload": (0, 0),
    "tab-list": (0, 0), "tab-new": (0, 1),
    "tab-close": (1, 1), "tab-select": (1, 1),
    "dialog-accept": (0, 1), "dialog-dismiss": (0, 0),
    "upload": (0, 1),
}
LEASE_SECONDS = 300
CONTROLLER_REVISION = "2026-11-17-readiness-and-release-11+monitor"
COMMAND_ALIASES = {"tab-switch": "tab-select", "switch-tab": "tab-select", "tabs": "tab-list"}


class DesktopUnavailable(ValueError):
    pass


class HumanPaused(ValueError):
    pass


class DesktopSessionFailed(ValueError):
    pass


def preparation_request(action, args):
    """Simple entry points to existing restricted preparation operations."""
    if action=='work-brief':
        if args:raise ValueError('Use work-brief without arguments.')
        return {'op':'work-brief'}
    if action == 'admit':
        if len(args) not in (1,2):raise ValueError('Use admit DRIVE_FILE_ID_OR_LINK [SAFE_NAME.pdf or SAFE_NAME.docx].')
        sys.path.insert(0,'/opt/muse-monitor')
        from muse_monitor.artifacts import drive_id
        from muse_monitor.store import Conflict
        try:source=drive_id(args[0])
        except (ValueError,Conflict):raise ValueError('Use admit DRIVE_FILE_ID_OR_LINK [SAFE_NAME.pdf or SAFE_NAME.docx].') from None
        return {'op': 'artifact-import', 'fileId': source, 'filename': args[1] if len(args)==2 else None}
    if action == 'artifacts':
        if args:
            raise ValueError('Use artifacts without arguments.')
        return {'op': 'artifact-list'}
    if action == 'tasks-help':
        if args:raise ValueError('Use tasks-help without arguments.')
        return {'op':'task','action':'guide'}
    if action in ('tasks', 'needs-you'):
        if args:raise ValueError('Use tasks or needs-you without arguments; use monitor-encoded for task updates.')
        return {'op':'task','action':'list','state':'needs-you' if action=='needs-you' else 'all'}
    return None


def capabilities():
    return {'revision': CONTROLLER_REVISION, 'leaseSeconds': LEASE_SECONDS,
            'browserCommands': list(REMOTE_COMMANDS), 'aliases': COMMAND_ALIASES,
            'tabSwitch': 'run TOKEN tab-select INDEX (refresh tab-list first)',
            'importPdf': 'admit DRIVE_FILE_ID_OR_LINK [SAFE_NAME.pdf or SAFE_NAME.docx]; omitted filename is derived from Drive',
            'fileTypes': ['PDF','DOCX','Google Doc exported as PDF'],
            'humanControl': 'Use the Muse Control desktop shortcut; agents pause while native file pickers are enabled',
            'readiness': 'status reports desktop and human-control gates without claiming end-to-end readiness; metadata and authenticated lease release work while the desktop is inactive',
            'taskWorkflow': {'read':'work-brief; tasks-help; tasks; needs-you; monitor-encoded JSON with op task or site',
                'actions':['guide','save','list','read','block','unblock','receipt','finish'],
                'guide':'TASK-WORKFLOW.md', 'topics':'Any subject; existing profiles are resources, not topic restrictions',
                'completion':'Each declared success criterion needs controller-checked page/file evidence or explicit human attestation'},
            'listPdfs': 'artifacts', 'listFiles': 'artifacts', 'uploadPdf': 'run TOKEN upload SAFE_NAME.pdf or SAFE_NAME.docx',
            'uploadProtocol': {'requiresOpenChooser': True,
                'flow': ['artifacts lists exact admitted PDF/DOCX names',
                         'snapshot then click the intended upload control once',
                         'upload SAFE_NAME.pdf or SAFE_NAME.docx',
                         'verify the website attachment receipt in a fresh snapshot'],
                'cancel': 'upload without a filename', 'shellFileTransferRequired': False},
            'tabScope': 'All open tabs in the assigned browser; other persistent profiles are separate. Use monitor status for workspaces.',
            'clearField': 'run TOKEN clear FRESH_REF',
            'readField': 'run TOKEN read-value FRESH_REF reads actual DOM value; use when a snapshot appears duplicated',
            'dialogSummary': 'snapshot surfaces visible Page dialogs and their existing control refs before the full tree; no automatic continuation',
            'requestDiagnostics': 'Private bounded request timing/outcome metadata includes failures before browser dispatch; no form values or tokens',
            'exactText': 'run-encoded TOKEN BASE64URL_JSON preserves empty strings and leading/trailing whitespace',
            'focusedInput': 'type TEXT / press KEY; optional type REF TEXT / press REF KEY focuses an exact fresh reference',
            'runCompatibility': ['help','capabilities','artifacts','admit DRIVE_FILE_ID SAFE_NAME.pdf'],
            'cancelChooser': 'run TOKEN upload', 'arbitraryShell': False,
            'forwarding': False, 'uploadLimitBytes': 8 * 1024**2}


def usable_gui(properties):
    # Enumerate actual GUI sessions instead of assuming show-user Display is
    # populated. Inactive GNOME/VMware rendering stalled in live qualification;
    # do not advertise availability merely because a user session still exists.
    return (properties.get('Type') in ('x11', 'wayland') and
            properties.get('Class') == 'user' and
            properties.get('State') in ('active', 'online') and
            properties.get('Active') == 'yes' and
            properties.get('LockedHint') == 'no')


def gui_available(username):
    sessions = subprocess.run(['loginctl', 'show-user', username, '-p', 'Sessions', '--value'],
                              text=True, capture_output=True, timeout=5, check=True).stdout.split()
    for session in sessions[:16]:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session):
            continue
        details = subprocess.run(['loginctl', 'show-session', session, '-p', 'Type',
                                 '-p', 'Class', '-p', 'State', '-p', 'Active', '-p', 'LockedHint'],
                                text=True, capture_output=True, timeout=5)
        if details.returncode == 0 and usable_gui(dict(line.split('=', 1) for line in details.stdout.splitlines() if '=' in line)):
            return True
    return False


def decode_json(encoded):
    if len(encoded) > 15500:
        raise ValueError('Encoded browser request exceeds its bound.')
    try:
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('Use bounded base64url JSON object transport.') from None


def decode_run(encoded):
    """Lossless SSH argument transport; the ordinary command allowlist still applies."""
    try:
        payload = decode_json(encoded)
        if set(payload) != {'command', 'args'} or not isinstance(payload['command'], str):
            raise ValueError()
        values = payload['args']
        if not isinstance(values, list) or not all(isinstance(v, str) and '\0' not in v and len(v) <= 4096 for v in values):
            raise ValueError()
        return payload['command'], values
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('Use base64url JSON containing only command and string args.') from None


def command_values(verb, values):
    # OpenSSH joins argv before the guest shell parses it. Preserve common
    # older clients' multiword fields; encoded requests preserve exact spacing,
    # quotes and newlines instead of reconstructing a whitespace-split tail.
    if verb in {'fill', 'select'} and len(values) > 2:
        values = [values[0], ' '.join(values[1:])]
    if verb == 'type' and len(values) > 1:
        if re.fullmatch(r'(?:f\d+)?e\d+', values[0]):
            values = [values[0], ' '.join(values[1:])]
        else:values = [' '.join(values)]
    if verb == 'press' and len(values) == 2 and not re.fullmatch(r'(?:f\d+)?e\d+',values[0]):
        raise ValueError('Use press KEY or press FRESH_REF KEY.')
    if verb == 'read-value' and (len(values)!=1 or not re.fullmatch(r'(?:f\d+)?e\d+',values[0])):
        raise ValueError('Use read-value FRESH_REF from the current snapshot.')
    if verb == 'fill' and len(values) == 1:
        raise ValueError('Fill value is missing; use clear REF, or run-encoded with an empty string. Shell argument joining can lose empty text.')
    limits = REMOTE_COMMANDS.get(verb)
    if not limits or not limits[0] <= len(values) <= limits[1] or any(v.startswith('-') or len(v) > 4096 for v in values):
        raise ValueError('Browser command or arguments are outside the allowed interface.')
    return values


def upload_artifact(home, value):
    config = json.loads((home / '.config/muse-monitor/config.json').read_text())
    folder = Path(config['state']) / 'artifacts'
    candidate = Path(value) if Path(value).is_absolute() else folder / value
    if candidate.is_symlink() or candidate.resolve().parent != folder.resolve():
        raise ValueError('Upload only an approved PDF in the private artifact directory.')
    from muse_monitor.artifacts import safe_filename,valid_artifact
    if not safe_filename(candidate.name):raise ValueError('Use an approved PDF/DOCX artifact filename.')
    if not candidate.is_file() or candidate.stat().st_size > 8 * 1024**2 or candidate.stat().st_uid != os.getuid():
        raise ValueError('Approved upload artifact is missing or exceeds 8 MiB.')
    if not valid_artifact(candidate.name,candidate.read_bytes()):raise ValueError('Upload artifact is not a supported PDF/DOCX file.')
    return str(candidate.resolve())


def cancel_chooser(cli):
    result = cli('upload')
    if result.returncode != 124 and ('requires an open file chooser' in result.stdout or 'related modal state present' in result.stdout):
        return subprocess.CompletedProcess(result.args,0,'### Result\n'+json.dumps({'chooserCancelled':False,'noChooserOpen':True,'existingTabsPreserved':True})+'\n')
    if result.returncode == 0 or "'files' argument:" not in result.stdout:
        return result
    # An already-running pinned daemon still has the old required-files CLI
    # schema. Use its fixed internal cancellation operation from a disposable
    # blank tab; do not restart Chrome or expose arbitrary code remotely.
    listing = cli('tab-list')
    selected = re.findall(r'(?m)^- (\d+): \(current\)', listing.stdout)
    if listing.returncode or len(selected) != 1:
        raise ValueError('Could not identify the selected chooser tab; no tab was closed.')
    opened = cli('tab-new', 'about:blank')
    if opened.returncode:
        return opened
    index = int(selected[0])
    code = '''async page => {
      if (page.url() !== 'about:blank') throw new Error('MUSE_CANCEL_NOT_OWN_TAB');
      const symbol = Object.getOwnPropertySymbols(page).find(s => s.description === 'tabSymbol');
      const temporary = symbol && page[symbol];
      if (!temporary) throw new Error('MUSE_CANCEL_PINNED_ADAPTER_CHANGED');
      const context = temporary.context;
      const target = context.tabs()[__INDEX__];
      try {
        if (!target || target === temporary) throw new Error('MUSE_CANCEL_TARGET_CHANGED');
        const states = target.modalStates().filter(s => s.type === 'fileChooser');
        if (states.length > 1) throw new Error('MUSE_CANCEL_AMBIGUOUS_CHOOSER');
        if (states.length === 1) target.clearModalState(states[0]);
        await context.selectTab(context.tabs().indexOf(target));
        return { chooserCancelled: states.length === 1, noChooserOpen: states.length === 0, existingTabsPreserved: true };
      } finally { await page.close(); }
    }'''.replace('__INDEX__', str(index))
    return cli('run-code', code)


@contextlib.contextmanager
def control_lock(runtime, remote, status=False, rejected=None):
    waiting = runtime / 'requests-waiting'
    waiting.mkdir(mode=0o700, exist_ok=True)
    marker = waiting / (str(os.getpid()) + '.json')
    if remote:
        write_state(marker, json.dumps({'pid': os.getpid(), 'started': time.monotonic()}))
    try:
        with (runtime / 'control.lock').open('a') as lock:
            deadline = time.monotonic() + 8
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if remote else 0))
                    break
                except BlockingIOError:
                    if status:
                        yield False
                        return
                    if time.monotonic() >= deadline:
                        if rejected:rejected(8000)
                        raise ValueError('Browser request still in progress; no new action was dispatched. Inspect before retrying.')
                    time.sleep(.025)
            marker.unlink(missing_ok=True)
            yield True
    finally:
        marker.unlink(missing_ok=True)


def request_history(runtime, event=None):
    """Small private record: command names/timings only, never inputs or output."""
    path = runtime / "request-history.json"
    try:
        history = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(history, list):
            history = []
        if event is not None:
            history = (history + [event])[-50:]
            write_state(path, json.dumps(history))
        return history
    except (OSError, ValueError):
        # A diagnostics failure must never turn a successful website action
        # into a failed command that a caller might submit again.
        return []


def show_diagnostics(runtime):
    print("Controller revision:", CONTROLLER_REVISION)
    print("Status lists browser processes; it does not prove a page is responsive.")
    events = request_history(runtime)
    actions = [event for event in events if event.get("command") != "list"]
    if actions:
        last = actions[-1]
        print(f"Last browser action: {last['command']}; exit={last['exit']}; "
              f"elapsed={last['seconds']}s; UTC={last['utc']}")
    try:
        current = json.loads((runtime / "current-request.json").read_text())
        elapsed = max(0, round(time.monotonic() - current["started"], 1))
        print(f"Request marker: {current['command']}; elapsed={elapsed}s; "
              f"limit={current['limit']}s (a terminated caller may leave a stale marker).")
    except (OSError, ValueError, KeyError):
        pass


def bounded_command(command, *, cwd, env, seconds):
    """Bound the CLI client without killing the detached browser daemon."""
    with subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True,
                          close_fds=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace") as child:
        try:
            output, _ = child.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            # The browser daemon is detached in its own process group. Terminate
            # only this request's CLI client so it cannot retain the control lock.
            os.killpg(child.pid, signal.SIGKILL)
            output, _ = child.communicate()
            return subprocess.CompletedProcess(command, 124, (output or "") +
                f"\nBrowser command timed out after {seconds}s; outcome is uncertain. "
                "Tabs were left open. Inspect current page/receipts before retrying.\n")
        return subprocess.CompletedProcess(command, child.returncode, output)


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f"Expected a private directory owned by this role: {path}")
    return path


def write_state(path, value):
    pending = path.with_suffix(".new")
    pending.write_text(value)
    pending.replace(path)


def run(argv):
    os.umask(0o077)
    account = pwd.getpwuid(os.getuid())
    if account.pw_uid == 0:
        raise ValueError("Run the browser as its assigned desktop account.")
    site = json.loads(Path(__file__).resolve().with_name("site.json").read_text())
    role = site["roles"].get(account.pw_name)
    if not role:
        raise ValueError("This Unix account has no browser role.")

    remote = argv == ["--ssh"]
    if remote:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        argv = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if not argv or (remote and len(" ".join(argv)) > 16000):
        raise ValueError("Specify a browser command.")
    action, *args = argv
    if action == 'capabilities' and not args:
        print(json.dumps(capabilities()))
        return 0
    prepared = preparation_request(action, args)
    if prepared:
        if prepared['op'] == 'artifact-list':
            sys.path.insert(0, '/opt/muse-monitor')
            from muse_monitor.artifacts import list_artifacts
            config = json.loads((Path(account.pw_dir) / '.config/muse-monitor/config.json').read_text())
            print(json.dumps(list_artifacts(config['state'])))
            return 0
        action, args = 'monitor', [json.dumps(prepared)]
    if action == 'monitor-encoded':
        if len(args) != 1:
            raise ValueError('Use monitor-encoded BASE64URL_JSON.')
        action, args = 'monitor', [json.dumps(decode_json(args[0]))]
    if action == 'run-encoded':
        if len(args) != 2:
            raise ValueError('Use run-encoded TOKEN BASE64URL_JSON.')
        verb, values = decode_run(args[1])
        action, args = 'run', [args[0], verb, *values]
    if action == "help" and not args:
        print(
            "Use status to inspect the browser, claim to obtain a controller token,\n"
            "run TOKEN COMMAND [ARGS] to use it, and release TOKEN when finished.\n"
            "For exact multiword/quoted text use run-encoded TOKEN BASE64URL_JSON,\n"
            "encoding {command: fill, args: [fresh_ref, exact_text]} as JSON.\n"
            "run TOKEN upload cancels an open file chooser without restarting Chrome.\n"
            "run TOKEN upload NAME.pdf attaches a private approved artifact;\n"
            "first click the intended upload control to open its chooser, then upload NAME.pdf.\n"
            "Verify the website attachment receipt afterward; no SSH file copy is required.\n"
            "artifacts lists admitted PDF/DOCX files with exact filenames, byte counts and hashes.\n"
            "admit DRIVE_FILE_ID_OR_LINK [SAFE_NAME.pdf or SAFE_NAME.docx] imports a Drive file; omitted names are derived automatically.\n"
            "Use artifact-import in monitor JSON to import an explicit approved Drive PDF/Doc first.\n"
            "run TOKEN tab-list lists indexes; run TOKEN tab-select INDEX switches tabs.\n"
            "tab-switch and switch-tab are aliases for tab-select; tabs aliases tab-list.\n"
            "Use capabilities for a machine-readable description of the installed interface.\n"
            "help, capabilities, artifacts and admit also work after run TOKEN for older helpers.\n"
            "Google Docs export as PDF. Native Word DOCX files are also supported; 8 MiB per file.\n"
            "The Muse Files desktop folder and file-picker bookmark show the same admitted files.\n"
            "work-brief combines tasks and saved site profiles/challenges in one read.\n"
            "tasks lists prepared work; needs-you lists exact human questions and stopping points.\n"
            "Use op task in monitor-encoded JSON for topic-independent packets, block/unblock and checked completion receipts.\n"
            "clear REF clears a field without relying on an empty shell argument.\n"
            "type TEXT / press KEY act on focus; optional type REF TEXT / press REF KEY focus a fresh ref first.\n"
            "Use run-encoded for exact leading/trailing spaces and empty fill values.\n"
            "Tab inventory is per assigned browser, never per lease; other workspaces remain separate.\n"
            "Snapshot Page dialogs summarizes visible dialogs before the full tree using the same refs.\n"
            "monitor-encoded BASE64URL_JSON transports an existing monitor request without shell quote loss.\n"
            "monitor diagnostics reads private bounded run/timing/error summaries.\n"
            "The token lasts five minutes and renews after commands.\n"
            "Claim once; a second claim fails while any lease is active.\n"
            "Commands have a time limit; exit 124 means inspect before retrying.\n"
            "A harmless test uses goto https://example.com/, snapshot, then screenshot.\n"
            "Screenshot returns JSON with mime_type and data_base64 fields containing a PNG.\n"
            "Decode it privately in the calling environment to view the guest screenshot.\n"
            f"Controller revision: {CONTROLLER_REVISION}. Status includes recent request timing.\n"
            "Muse Monitor integration: monitor JSON forwards bounded requests as the existing authenticated Main identity.\n"
            "Remote browser commands include " + ", ".join(REMOTE_COMMANDS) + ".\n"
            "Pause, resume, start, and stop are local desktop controls.\n"
            "Keep Muse selected in VMware; use Muse operator's separate RDP desktop. Human pauses remain authoritative."
        )
        return 0
    manual_url = None
    if not remote and action == "browse":
        if len(args) > 1:
            raise ValueError("Open one website at a time.")
        if args:
            manual_url = args[0]
            url = urlsplit(manual_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
                raise ValueError("Use an HTTP(S) URL without embedded credentials.")
        action, args = "start", []

    user_runtime = Path(f"/run/user/{account.pw_uid}")
    if not user_runtime.is_dir():
        if action == "status" and not args:
            print("Automation: offline (role desktop has no runtime)")
            return 0
        raise ValueError("Sign in to the role's Ubuntu desktop before controlling its browser.")
    home = Path(account.pw_dir)
    base = private_dir(Path(site.get("checkpoint", home / ".local/share/muse-browser")))
    profile = private_dir(base / "profile")
    runtime = private_dir(user_runtime / f"muse-browser-{role}")
    paused, stopped, lease = (base / "paused", base / "stopped", runtime / "lease")
    cwd = private_dir(runtime / "cli")
    config = runtime / "config.json"
    env = {key: value for key, value in os.environ.items()
           if key in {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                      "XDG_SESSION_TYPE", "LANG", "LC_ALL", "XDG_CURRENT_DESKTOP"}}
    env.update(HOME=str(home), PATH=f"{site['node_bin']}:/usr/bin:/bin",
               XDG_RUNTIME_DIR=str(runtime.parent),
               XDG_CACHE_HOME=str(private_dir(base / "cache")))
    binary = Path(site["node_bin"]) / "playwright-cli"
    # Installed optional integration, not a new remote shell/capability.
    sys.path.insert(0, "/opt/muse-monitor")
    try:
        from muse_monitor.legacy import guard, admission, legacy_capacity, local_resume
    except ImportError:
        guard = lambda runtime: None
        admission = lambda runtime: contextlib.nullcontext()
        legacy_capacity = lambda runtime: None
        local_resume = lambda: None
    from muse_monitor.diagnostics import Diagnostics
    from muse_monitor.daemon import execute,DispatchUncertain
    trace=Diagnostics(home/'.local/share/muse-monitor/diagnostics')
    workspace={'main':'jobs'}.get(role,role)

    def trace_run():
        try:
            value=json.loads(lease.read_text())
            return value.get('runId') or __import__('hashlib').sha256(value.get('token','').encode()).hexdigest()[:16]
        except (OSError,ValueError):return None

    if action == "monitor" and len(args) == 1:
        from muse_monitor.cli import request
        payload={"op":args[0]} if args[0] in {"capabilities","status","health","next-work","google-status","diagnostics"} else json.loads(args[0])
        if not isinstance(payload,dict):raise ValueError("Bounded JSON object required.")
        print(json.dumps(request(home / ".config/muse-monitor/config.json",payload)))
        return 0

    # Read-only status must show all gates, even when a switched-away desktop
    # prevents browser work. Releasing one's own lease must not need a GUI at
    # all; it remains authenticated by the existing token under control_lock.
    if remote and action not in {"monitor", "release"}:
        desktop_ready = gui_available(account.pw_name)
        session_failed = desktop_ready and subprocess.run(
            ["pgrep", "-u", str(account.pw_uid), "-x", "gnome-session-f"],
            stdout=subprocess.DEVNULL).returncode == 0
        blockers = []
        if paused.exists():blockers.append('HUMAN_PAUSED')
        if stopped.exists():blockers.append('BROWSER_STOPPED')
        if (base/'manual-login.json').exists():blockers.append('HUMAN_SIGN_IN')
        if not desktop_ready:blockers.append('DESKTOP_UNAVAILABLE')
        if session_failed:blockers.append('DESKTOP_SESSION_FAILED')
        if action == 'status' and not args:
            state = 'stopped' if stopped.exists() else 'paused' if paused.exists() else 'unavailable' if not desktop_ready or session_failed else 'available'
            print('Automation:', state)
            print('Readiness:', json.dumps({'revision':CONTROLLER_REVISION,
                'desktopReady':bool(desktop_ready and not session_failed),
                'admissionBlocked':bool(blockers),'blockers':blockers,
                'scope':'Desktop and human-control gates only; not an end-to-end browser/SSH test'}))
            if not desktop_ready or session_failed:return 0
        else:
            if paused.exists() or stopped.exists():
                raise HumanPaused('MUSE_HUMAN_PAUSED: Automation is paused or stopped. Only the desktop operator can resume it using Muse Control > Let Muse work when finished.')
            if not desktop_ready:
                raise DesktopUnavailable('MUSE_DESKTOP_UNAVAILABLE: Muse GUI is inactive, signed out or locked. Preserve the current session; restore the Muse desktop before browser work. Metadata preparation and lease release remain available.')
            if session_failed:
                raise DesktopSessionFailed('MUSE_DESKTOP_SESSION_FAILED: Muse desktop reported a session failure. Recover its desktop before browser work.')

    if (base / 'manual-login.json').exists() and action in {'claim','run','start','autostart','stop','resume'}:
        raise ValueError('Human sign-in is active. Use the workspace Finish human sign-in action before reconnecting automation.')

    if not remote and action == "resume" and not args and not stopped.exists():
        local_resume()

    if not remote and action=='pause' and not args and (home/'.config/muse-monitor/config.json').exists():
        from muse_monitor.human_login import human
        human(home/'.config/muse-monitor/config.json',workspace,'pause')
        print('Human control enabled; agents paused and the native file picker restored.')
        return 0

    if action == "status" and not args:
        show_diagnostics(runtime)

    if manual_url or (not remote and not args and action in {"pause", "stop"}):
        paused.touch()
    admission_started=time.monotonic()
    with control_lock(runtime, remote, action == 'status' and not args,
                      lambda ms:trace.emit('admission','legacy',workspace=workspace,actor='main',runId=trace_run(),command=action,lockWaitMs=ms,outcome='error',errorType='AdmissionBusy')) as acquired:
        if not acquired:
            print('Automation: busy (another browser request is in progress).')
            return 0
        waited=(time.monotonic()-admission_started)*1000
        if waited>100:trace.emit('admission','legacy',workspace=workspace,actor='main',runId=trace_run(),command=action,lockWaitMs=waited)

        selected_url=None
        def cli(*command, named=True):
            nonlocal selected_url
            session = [f"-s={role}"] if named else []
            seconds = 8 if command[0] == "list" else 45 if command[0] == "open" else 35
            started = time.monotonic()
            pending = runtime / "current-request.json"
            try:
                write_state(pending, json.dumps({"command": command[0],
                    "started": started, "limit": seconds}))
            except OSError:
                pass
            try:
                transport='daemon'
                try:result=execute(base,role,cwd,command,seconds) if command[0] not in ('open','close') else None
                except DispatchUncertain:result=subprocess.CompletedProcess(command,124,'Browser outcome uncertain after daemon disconnect/deadline; inspect before retrying.\n')
                if result is None:
                    transport='node'
                    result = bounded_command([str(binary), *session, *command],
                                             cwd=cwd, env=dict(env,NO_UPDATE_NOTIFIER='1'), seconds=seconds)
                if result.returncode == 0 and '### Error' in result.stdout:
                    result = subprocess.CompletedProcess(result.args, 1, result.stdout)
                if getattr(result,'muse_current_url',None):selected_url=result.muse_current_url
                request_history(runtime, {"command": command[0],
                    "seconds": round(time.monotonic() - started, 2),
                    "exit": result.returncode,
                    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                if command[0]!='list':trace.emit('command','legacy',workspace=workspace,actor='main',runId=trace_run(),command=command[0],elapsedMs=(time.monotonic()-started)*1000,transport=transport,outcome='ok' if result.returncode==0 else 'uncertain' if result.returncode==124 else 'error',status=result.returncode,**getattr(result,'muse_timing',{}))
                return result
            finally:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass

        def show(result):
            print(result.stdout, end="")
            return result.returncode

        if manual_url:
            lease.unlink(missing_ok=True)

        if action in {"claim", "run", "start", "stop"}:
            guard(runtime)

        if action == "status" and not args:
            state = "paused" if paused.exists() else "available"
            if not remote:print("Automation:", "stopped" if stopped.exists() else state)
            return show(cli("list", named=False))

        # Releasing control must not depend on a responsive Chrome/CLI daemon.
        if action == "release" and len(args) == 1:
            saved = json.loads(lease.read_text()) if lease.exists() else {}
            if not secrets.compare_digest(args[0], saved.get("token", "")):
                raise ValueError("Browser lease belongs to a different controller or was cleared.")
            trace.emit('lease_end','legacy',workspace=workspace,actor='main',runId=trace_run(),outcome='released')
            lease.unlink()
            print("Browser control released.")
            return 0

        if not remote and not args:
            if action in {"pause", "stop"}:
                paused.touch()
                lease.unlink(missing_ok=True)
                if action == "pause":
                    print("Paused. You can take over the visible browser now.")
                    return 0
                stopped.touch()
                return show(cli("close"))
            if action == "resume":
                if stopped.exists():
                    raise ValueError("Start the browser before resuming automation.")
                lease.unlink(missing_ok=True)
                paused.unlink(missing_ok=True)
                print("Automation available for a new claim.")
                return 0
            if action in {"start", "autostart"}:
                if action == "autostart" and stopped.exists():
                    return 0
                if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                    raise ValueError("Start from the role's logged-in Ubuntu desktop.")
                if shutil.disk_usage(profile).free < 512 * 1024**2:
                    raise ValueError("Less than 512 MiB free. Free disk space before starting Chrome.")
                current = cli("list", named=False)
                if current.returncode:
                    return show(current)
                if re.search(rf"(?m)^- {re.escape(role)}:\s*\n  - status: open$", current.stdout):
                    stopped.unlink(missing_ok=True)
                    print("Browser is already open.")
                    return show(cli("tab-new", manual_url)) if manual_url else 0
                write_state(config, json.dumps({
                    "browser": {"launchOptions": {
                        "executablePath": site["chrome"], "chromiumSandbox": True,
                        "ignoreDefaultArgs": ["--password-store=basic", "--use-mock-keychain",
                                              "--disable-blink-features=AutomationControlled",
                                              "--enable-unsafe-swiftshader",
                                              "--unsafely-disable-devtools-self-xss-warnings"],
                        "args": ["--disable-sync", "--enable-automation", "--class=Muse-" + role.capitalize()],
                    }},
                    "outputMode": "stdout",
                    "timeouts": {"action": 10000, "navigation": 30000},
                }))
                result = cli("open", "about:blank", "--headed", f"--profile={profile}", f"--config={config}")
                if result.returncode == 0:
                    stopped.unlink(missing_ok=True)
                    lease.unlink(missing_ok=True)
                    from muse_monitor.desktop_mode import apply_mode
                    mode=apply_mode(cli,True)
                    if mode.returncode:return show(mode)
                code = show(result)
                return show(cli("tab-new", manual_url)) if manual_url and code == 0 else code

        if paused.exists() or stopped.exists():
            raise HumanPaused("MUSE_HUMAN_PAUSED: Automation is paused or stopped. Only the desktop operator can resume it using Muse Control > Let Muse work when finished.")
        if shutil.disk_usage(profile).free < 256 * 1024**2:
            raise ValueError("Less than 256 MiB free. Pause automation and free disk space.")
        current = cli("list", named=False)
        if current.returncode or not re.search(rf"(?m)^- {re.escape(role)}:\s*\n  - status: open$", current.stdout):
            raise ValueError("Browser is closed. Start it from the role's logged-in Ubuntu desktop.")

        now = time.monotonic()
        saved = json.loads(lease.read_text()) if lease.exists() else {}
        active = saved.get("until", 0) > now
        if action == "claim" and not args:
            with admission(runtime):
                guard(runtime)
                legacy_capacity(runtime)
                if active:
                    raise ValueError("Another controller holds the browser lease.")
                from muse_monitor.desktop_mode import apply_mode
                mode=apply_mode(cli,False)
                if mode.returncode:return show(mode)
                token = secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789') + secrets.token_urlsafe(24)[1:]
                run_id=__import__('hashlib').sha256(token.encode()).hexdigest()[:16]
                write_state(lease, json.dumps({"token": token, "until": now + LEASE_SECONDS,'runId':run_id}))
                trace.emit('lease_start','legacy',workspace=workspace,actor='main',runId=run_id)
            print(token)
            return 0
        if action not in {"release", "run"} or not args:
            raise ValueError("Use status, claim, run TOKEN COMMAND [ARGS], or release TOKEN.")
        token, *args = args
        if not active or not secrets.compare_digest(token, saved["token"]):
            raise ValueError("Browser lease expired or belongs to a different controller.")
        if not saved.get('runId'):
            saved['runId']=__import__('hashlib').sha256(token.encode()).hexdigest()[:16]
            write_state(lease,json.dumps(saved))
            trace.emit('lease_observed','legacy',workspace=workspace,actor='main',runId=saved['runId'])
        if action == "release" and not args:
            lease.unlink()
            return 0
        if action != "run" or not args:
            raise ValueError("Specify a permitted browser command.")
        verb, *values = args
        verb = COMMAND_ALIASES.get(verb, verb)
        if verb in {'help','capabilities','artifacts','admit'}:
            # Only these bounded existing preparation/help operations may pass
            # through an older helper that always prefixes run TOKEN.
            try:code=run([verb,*values])
            except ValueError as error:
                print('MUSE_ERROR [PREPARATION_USAGE]: '+str(error))
                code=2
            write_state(lease,json.dumps({**saved,'until':time.monotonic()+LEASE_SECONDS}))
            return code
        values = command_values(verb, values)
        from muse_monitor.legacy_actions import inventory,focus_input,read_value,describe_error,decorate_snapshot,guard_site
        from muse_monitor.cli import request
        guard_site(verb,values,selected_url,cli,lambda target:request(home/'.config/muse-monitor/config.json',{'op':'site','action':'check','workspace':workspace,'url':target}))
        if verb == 'clear':verb,values='fill',[values[0],'']
        if verb == 'upload' and values:
            values = [upload_artifact(home, values[0])]
        if verb in {"goto", "tab-new"} and values:
            url = urlsplit(values[0])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
                raise ValueError("Use an HTTP(S) URL without embedded credentials.")
        if verb == 'tab-list':
            result=inventory(cli);show(describe_error(verb,result))
        elif verb == 'tab-select':
            if not re.fullmatch(r'\d{1,4}',values[0]):raise ValueError('Select a nonnegative index from a fresh tab-list.')
            result=inventory(cli,int(values[0]));show(describe_error(verb,result))
        elif verb in {'type','press'} and len(values)==2:
            result=focus_input(cli,verb,*values);show(describe_error(verb,result))
        elif verb == 'read-value':
            result=read_value(cli,values[0]);show(describe_error(verb,result))
        elif verb == 'snapshot':
            result=cli('snapshot')
            if result.returncode==0:
                tabs=inventory(cli)
                result=decorate_snapshot(result,tabs)
            show(describe_error(verb,result))
        elif verb == 'upload' and not values:
            result = cancel_chooser(cli)
            show(describe_error(verb,result))
        elif verb == "screenshot":
            with tempfile.TemporaryDirectory(prefix="capture-", dir=runtime) as capture:
                screenshot = Path(capture) / "page.png"
                result = cli("screenshot", f"--filename={screenshot}", "--type=png")
                if result.returncode == 0:
                    if screenshot.stat().st_size > 8 * 1024**2:
                        raise ValueError("Screenshot exceeds the 8 MiB transfer limit.")
                    content = screenshot.read_bytes()
                    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("Browser did not return a PNG screenshot.")
                    print(json.dumps({
                        "mime_type": "image/png",
                        "data_base64": base64.b64encode(content).decode("ascii"),
                    }))
                else:
                    show(result)
        else:
            result = cli(verb, *values)
            show(describe_error(verb,result))
        write_state(lease, json.dumps({**saved,"token": token, "until": time.monotonic() + LEASE_SECONDS}))
        return result.returncode


def request_operation(argv):
    """Fixed operation label only; never log a raw command, token or payload."""
    try:
        if argv==['--ssh']:
            original=os.environ.get('SSH_ORIGINAL_COMMAND','')
            if len(original)>16000:return 'invalid'
            argv=shlex.split(original)
        if not argv:return 'invalid'
        action=argv[0]
        if action=='run' and len(argv)>=3:action=argv[2]
        elif action=='run-encoded' and len(argv)==3:action=decode_run(argv[2])[0]
        elif action=='monitor-encoded':action='monitor'
        action=COMMAND_ALIASES.get(action,action)
        return action if action in REMOTE_COMMANDS or action in {'status','claim','release','monitor','capabilities','help','artifacts','admit','pause','resume','start','stop','autostart','browse'} else 'invalid'
    except (ValueError,TypeError,IndexError):return 'invalid'


def request_logging():
    account=pwd.getpwuid(os.getuid())
    site=json.loads(Path(__file__).resolve().with_name('site.json').read_text())
    role=site['roles'].get(account.pw_name)
    if not role:return None,None
    sys.path.insert(0,'/opt/muse-monitor')
    from muse_monitor.diagnostics import Diagnostics
    return Diagnostics(Path(account.pw_dir)/'.local/share/muse-monitor/diagnostics'),Path('/run/user')/str(account.pw_uid)/('muse-browser-'+role)/'lease'


def request_run_id(path):
    if path is None:return None
    try:
        data=json.loads(path.read_text())
        value=data.get('runId','') if isinstance(data,dict) else ''
        return value if isinstance(value,str) and re.fullmatch('[0-9a-f]{16}',value) else None
    except (OSError,ValueError,TypeError):return None


def main(argv):
    started=time.monotonic();code=1;error_type=None;trace=lease_path=None
    try:trace,lease_path=request_logging()
    except Exception:pass # Optional diagnostics must never fail an action.
    run_id=request_run_id(lease_path)
    try:
        result=run(argv);code=0 if result is None else result
        return code
    except (OSError, ValueError) as error:
        error_type=type(error).__name__
        print(error, file=sys.stderr)
        return 1
    finally:
        if trace is not None:
            try:
                operation=request_operation(argv)
                observed_id=run_id if operation=='release' else request_run_id(lease_path) or run_id
                trace.emit('request','legacy-handler',command=operation,
                           runId=observed_id,
                           elapsedMs=(time.monotonic()-started)*1000,status=code,
                           outcome='ok' if code==0 else 'uncertain' if code==124 else 'error',
                           errorType=error_type)
            except Exception:pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

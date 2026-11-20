import importlib.util,subprocess,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('rdp_ready',Path(__file__).parents[1]/'rdp-startup-ready.py')
ready=importlib.util.module_from_spec(spec);spec.loader.exec_module(ready)
try:
    from gi.repository import Gio,GLib
except ImportError:
    Gio=GLib=None


class StartupDeadlineTests(unittest.TestCase):
    def test_waits_for_readiness_then_stops_without_repeating_work(self):
        now=[0.0];probe=Mock();probe.check.side_effect=[False,False,True]
        result=ready.wait_for_gdm(probe,timeout=1,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s))
        self.assertTrue(result['ready']);self.assertEqual(result['attempts'],3)
        self.assertAlmostEqual(now[0],.4)

    def test_absent_gdm_is_bounded_and_never_reported_ready(self):
        now=[0.0];probe=Mock();probe.check.return_value=False
        result=ready.wait_for_gdm(probe,timeout=.55,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s))
        self.assertFalse(result['ready']);self.assertAlmostEqual(now[0],.55)
        self.assertEqual(result['attempts'],3)

    def test_probe_deadline_cost_cannot_trigger_another_attempt(self):
        now=[0.0];probe=Mock()
        def stalled(deadline):now[0]=deadline;return False
        probe.check.side_effect=stalled
        result=ready.wait_for_gdm(probe,timeout=.2,clock=lambda:now[0],sleep=lambda s:self.fail('No sleep after deadline'))
        self.assertFalse(result['ready']);self.assertEqual(result['attempts'],1)


@unittest.skipIf(Gio is None,'Run native D-Bus acceptance with /usr/bin/python3 (system PyGObject)')
class NativeBusTests(unittest.TestCase):
    def test_name_alone_and_partial_interfaces_fail_then_delayed_ready_succeeds_without_login(self):
        daemon=subprocess.Popen(['dbus-daemon','--session','--nofork','--print-address=1'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        address=daemon.stdout.readline().strip();connections=[];loop=GLib.MainLoop();thread=threading.Thread(target=loop.run,daemon=True);thread.start()
        registrations=[];calls=[]
        try:
            def connect():
                c=Gio.DBusConnection.new_for_address_sync(address,Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
                connections.append(c);return c
            server=connect();probe=ready.GdmProbe(connect())
            # NO_AUTO_START and missing-owner handling: the private bus has no
            # GDM and the probe must not attempt a session/login operation.
            self.assertFalse(probe.check(time.monotonic()+1))
            server.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(ready.GDM,0)),None,Gio.DBusCallFlags.NONE,1000,None)
            self.assertFalse(probe.check(time.monotonic()+1))
            factory_xml='<node><interface name="org.gnome.DisplayManager.RemoteDisplayFactory"><method name="CreateRemoteDisplay"><arg name="id" type="s" direction="in"/></method></interface></node>'
            def factory(c,s,p,i,m,params,invocation):
                calls.append(m);invocation.return_dbus_error('org.gnome.DisplayManager.Forbidden','The readiness check must never create a session')
            registrations.append(server.register_object(ready.FACTORY,Gio.DBusNodeInfo.new_for_xml(factory_xml).interfaces[0],factory,None,None))
            self.assertFalse(probe.check(time.monotonic()+1))
            objects_xml='<node><interface name="org.freedesktop.DBus.ObjectManager"><method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method></interface></node>'
            def managed(c,s,p,i,m,params,invocation):
                calls.append(m);invocation.return_value(GLib.Variant('(a{oa{sa{sv}}})',({},)))
            def later():
                registrations.append(server.register_object(ready.DISPLAYS,Gio.DBusNodeInfo.new_for_xml(objects_xml).interfaces[0],managed,None,None));return False
            GLib.timeout_add(100,later)
            result=ready.wait_for_gdm(probe,timeout=2,poll=.03)
            self.assertTrue(result['ready']);self.assertGreater(result['attempts'],1)
            self.assertEqual(calls,['GetManagedObjects'])
            server.unregister_object(registrations.pop())
            self.assertFalse(probe.check(time.monotonic()+1))
        finally:
            for c in connections:
                try:c.close_sync(None)
                except Exception:pass
            loop.quit();thread.join(2);daemon.terminate();daemon.wait(3);daemon.stdout.close()


if __name__=='__main__':unittest.main()

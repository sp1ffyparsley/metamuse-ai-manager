#!/usr/bin/python3
"""One-shot, read-only GDM admission check for the native system RDP unit.

No credentials, session creation, service restarts or background loop. Uses the
same existing system-bus interfaces as GNOME Remote Desktop, without activation.
"""
import sys,time,xml.etree.ElementTree as ET

GDM='org.gnome.DisplayManager'
FACTORY='/org/gnome/DisplayManager/RemoteDisplayFactory'
DISPLAYS='/org/gnome/DisplayManager/Displays'


class GdmProbe:
    def __init__(self,connection=None,clock=time.monotonic):
        from gi.repository import Gio,GLib
        self.Gio,self.GLib=Gio,GLib
        self.bus=connection or Gio.bus_get_sync(Gio.BusType.SYSTEM,None)
        self.clock=clock

    def check(self,deadline):
        def call(destination,path,interface,method,params=None):
            remaining=deadline-self.clock()
            if remaining<=0:raise TimeoutError()
            return self.bus.call_sync(destination,path,interface,method,params,None,
                self.Gio.DBusCallFlags.NO_AUTO_START,max(1,min(500,int(remaining*1000))),None).unpack()[0]
        def owner():
            return call('org.freedesktop.DBus','/org/freedesktop/DBus',
                'org.freedesktop.DBus','GetNameOwner',self.GLib.Variant('(s)',(GDM,)))
        try:
            initial_owner=owner()
            xml=call(GDM,FACTORY,'org.freedesktop.DBus.Introspectable','Introspect')
            root=ET.fromstring(xml)
            if not any(i.get('name')=='org.gnome.DisplayManager.RemoteDisplayFactory' and
                       any(m.get('name')=='CreateRemoteDisplay' for m in i.findall('method'))
                       for i in root.findall('interface')):return False
            objects=call(GDM,DISPLAYS,'org.freedesktop.DBus.ObjectManager','GetManagedObjects')
            return isinstance(objects,dict) and initial_owner==owner()
        except (self.GLib.Error,ET.ParseError,TimeoutError,TypeError,IndexError):
            return False


def wait_for_gdm(probe,timeout=12,poll=.2,clock=time.monotonic,sleep=time.sleep):
    start=clock();deadline=start+timeout;attempts=0
    while clock()<deadline:
        attempts+=1
        if probe.check(deadline):return {'ready':True,'attempts':attempts,'elapsedMs':round((clock()-start)*1000,2)}
        remaining=deadline-clock()
        if remaining>0:sleep(min(poll,remaining))
    return {'ready':False,'attempts':attempts,'elapsedMs':round((clock()-start)*1000,2)}


def main():
    try:
        from gi.repository import GLib
    except ImportError:
        result={'ready':False}
    else:
        try:result=wait_for_gdm(GdmProbe())
        except (GLib.Error,OSError):result={'ready':False}
    if result['ready']:
        print('RDP startup: GDM interfaces ready; no sessions or credentials changed.')
        return 0
    print('MUSE_RDP_STARTUP_NOT_READY: GDM interfaces unavailable; refusing partial startup.',file=sys.stderr)
    return 1


if __name__=='__main__':sys.exit(main())

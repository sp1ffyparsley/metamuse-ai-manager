import json,os,socket,tempfile,threading,time,unittest
from pathlib import Path
from muse_monitor.daemon import execute,DispatchUncertain,PINNED_VERSION
from muse_monitor.diagnostics import Diagnostics
from muse_monitor.store import Store

class DiagnosticsTests(unittest.TestCase):
    def test_rotating_logs_are_private_bounded_and_do_not_record_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Diagnostics(Path(tmp)/'logs',max_bytes=700,backups=2)
            for i in range(60):log.emit('command','fixture',command='fill',elapsedMs=i,outcome='error' if i%10==0 else 'ok',password='DO_NOT_LOG',url='https://private.example/',text='PRIVATE_FORM',errorType='message with private data')
            files=list(log.root.glob('events.jsonl*'))
            self.assertEqual(len(files),3)
            text=''.join(p.read_text() for p in files)
            self.assertNotIn('DO_NOT_LOG',text);self.assertNotIn('PRIVATE_FORM',text);self.assertNotIn('private.example',text)
            self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in files))
            self.assertTrue(all(p.stat().st_size<1000 for p in files))
            summary=log.summary();self.assertGreater(summary['commandCount'],0);self.assertFalse(summary['contentsLogged'])
    def test_diagnostic_io_failure_does_not_fail_an_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid=Path(tmp)/'file';invalid.write_text('not a directory')
            Diagnostics(invalid).emit('command','fixture') # must not raise
    def test_repeated_identical_incident_does_not_write_a_heartbeat_every_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            store.incident('browser','browser','jobs',{'reason':'Responsive'},resolved=True)
            before=store.db.total_changes
            for _ in range(10):store.incident('browser','browser','jobs',{'reason':'Responsive'},resolved=True)
            self.assertEqual(before,store.db.total_changes)
            store.incident('browser','browser','jobs',{'reason':'Unavailable'})
            self.assertGreater(store.db.total_changes,before);store.db.close()

class DaemonTests(unittest.TestCase):
    def call(self,mode='ok',insecure=False,command=None):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);endpoint=base/'rpc.sock'
            listener=socket.socket(socket.AF_UNIX);listener.bind(str(endpoint));os.chmod(endpoint,0o600);listener.listen();listener.settimeout(.3)
            folder=base/'cache/ms-playwright/daemon/fixture';folder.mkdir(parents=True)
            meta=folder/'main.session';meta.write_text(json.dumps({'name':'main','version':PINNED_VERSION,'socketPath':str(endpoint)}));meta.chmod(0o644 if insecure else 0o600)
            received=[]
            def server():
                try:
                    conn,_=listener.accept()
                    with conn:
                        data=bytearray()
                        while b'\n' not in data:
                            chunk=conn.recv(65536)
                            if not chunk:return
                            data.extend(chunk)
                        r=json.loads(data);received.append(r)
                        if mode=='disconnect':return
                        if mode=='timeout':time.sleep(.12);return
                        reply_text='### Error\nFixture error' if mode=='error' else '### Open tabs\n- 0: (current) private title (https://private.example)' if mode=='list-open' else 'No open tabs' if mode=='list-closed' else '### Result\n"observed"'
                        value={'id':r['id'],'result':{'isError':mode=='error','text':reply_text}}
                        conn.sendall((json.dumps(value)+'\n').encode())
                except socket.timeout:pass
            worker=threading.Thread(target=server);worker.start()
            try:
                if mode in ('disconnect','timeout'):
                    with self.assertRaises(DispatchUncertain):execute(base,'main',base,['fill','e1','two "quoted" words'],.05)
                    result=None
                else:result=execute(base,'main',base,command or ['fill','e1','two "quoted" words'],1)
            finally:worker.join(1);listener.close()
            return result,received
    def test_private_daemon_keeps_exact_arguments_without_a_node_process(self):
        result,received=self.call();self.assertEqual(result.returncode,0)
        self.assertEqual(received[0]['params']['args']['_'],['fill','e1','two "quoted" words'])
    def test_dispatched_disconnect_and_timeout_are_not_replayed(self):
        for mode in ('disconnect','timeout'):
            _,received=self.call(mode);self.assertEqual(len(received),1)
    def test_unsafe_metadata_falls_back_before_sending_anything(self):
        result,received=self.call(insecure=True);self.assertIsNone(result);self.assertEqual(received,[])
    def test_daemon_tool_error_is_a_failure_exit(self):
        result,_=self.call('error');self.assertEqual(result.returncode,1)
    def test_alive_daemon_does_not_claim_a_closed_browser_is_open(self):
        for mode,expected in (('list-open','open'),('list-closed','closed'),('error','closed')):
            result,received=self.call(mode,command=['list'])
            self.assertEqual(received[0]['params']['args']['_'],['tab-list'])
            self.assertIn('status: '+expected+'\n',result.stdout)
            self.assertNotIn('private',result.stdout)

if __name__=='__main__':unittest.main()

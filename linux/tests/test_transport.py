import importlib.machinery,importlib.util,json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch

path=Path(__file__).parents[2]/'client-tools/guest.sh'
loader=importlib.machinery.SourceFileLoader('guest_transport',str(path))
spec=importlib.util.spec_from_loader(loader.name,loader);transport=importlib.util.module_from_spec(spec);loader.exec_module(transport)


class TransportTests(unittest.TestCase):
    def test_public_host_pin_and_exact_ssh_argument_quoting(self):
        self.assertEqual(transport.fingerprint(transport.PUBLIC_KEY),transport.FINGERPRINT)
        args=['run','dummy-token','fill','e1',''," leading 'quotes'\ntrailing "]
        self.assertEqual(transport.shlex.split(transport.shlex.join(args)),args)

    def test_proxy_configuration_stays_in_runtime_environment(self):
        scheme,host,userinfo=transport.proxy_endpoint({'HTTPS_PROXY':'http://fake-user:fake-password@proxy.example:8080'})
        self.assertEqual((scheme,host,userinfo),('http','proxy.example','fake-user:fake-password'))
        self.assertEqual(transport.PROXY_PORT,3130)
        with self.assertRaises(ValueError):transport.proxy_endpoint({})

    def test_private_state_host_pin_and_existing_key_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key=root/'existing-key';key.write_text('fixture key path only')
            with patch.object(transport,'STATE',root/'state'),patch.object(transport,'KEY',key):
                t=transport.Transport();self.assertEqual(t.known.stat().st_mode&0o777,0o600)
                self.assertEqual(t.control.parent.stat().st_mode&0o777,0o700)
                t.known.write_text('changed pin\n')
                with self.assertRaises(ValueError):transport.Transport()
                self.assertEqual(t.known.read_text(),'changed pin\n')
                t.known.unlink();t.known.symlink_to(key)
                with self.assertRaises(ValueError):transport.Transport()
                self.assertEqual(key.read_text(),'fixture key path only')

    def test_unknown_command_outcome_never_creates_a_fallback_connection_or_replays(self):
        t=transport.Transport.__new__(transport.Transport);t.ensure_master=Mock();t.base=Mock(return_value=['ssh'])
        proc=Mock();proc.wait.return_value=255
        with patch.object(transport.subprocess,'Popen',return_value=proc) as launch:
            self.assertEqual(t.command(['run','fixture','click','e1']),255)
        self.assertEqual(launch.call_count,1)
        self.assertIn('ProxyCommand=false',launch.call_args.args[0]);t.ensure_master.assert_called_once()

    def test_timeout_terminates_only_client_and_is_not_replayed(self):
        t=transport.Transport.__new__(transport.Transport);t.ensure_master=Mock();t.base=Mock(return_value=['ssh'])
        proc=Mock();proc.wait.side_effect=[subprocess.TimeoutExpired('ssh',.1),0]
        with patch.object(transport.subprocess,'Popen',return_value=proc) as launch:
            self.assertEqual(t.command(['snapshot'],timeout=.1),124)
        self.assertEqual(launch.call_count,1);proc.terminate.assert_called_once()


if __name__=='__main__':unittest.main()

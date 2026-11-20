import json,subprocess,unittest
from unittest.mock import Mock
from muse_monitor.legacy_actions import inventory,decorate_snapshot,describe_error,script,native_inventory,read_value,focus_input,dialog_summary


def result(value,code=0):return subprocess.CompletedProcess([],code,'### Result\n'+json.dumps(value)+'\n')


class LegacyActionTests(unittest.TestCase):
    def test_every_inventory_is_complete_and_select_reports_new_current_tab(self):
        entries=[{'index':0,'current':False,'title':'existing','url':'https://example.com/old'},
                 {'index':1,'current':True,'title':'new','url':'https://example.com/new'}]
        cli=Mock(return_value=result({'tabs':entries,'selectedIndex':1}))
        for _ in range(3):
            r=inventory(cli);self.assertIn('- 0: [existing]',r.stdout);self.assertIn('- 1: (current)',r.stdout)
        cli=Mock(side_effect=[subprocess.CompletedProcess([],0,'### Result\nold misleading native output'),result({'tabs':entries,'selectedIndex':1}),result({'tabs':entries,'selectedIndex':1})])
        r=inventory(cli,1);self.assertEqual(cli.call_args_list[0].args,('tab-select','1'))
        self.assertIn('"selectedIndex": 1',r.stdout);self.assertNotIn('misleading',r.stdout)

    def test_native_full_result_avoids_action_settle_and_does_not_invent_modal_state(self):
        raw=subprocess.CompletedProcess([],0,'### Result\n- 0: [old](https://example.com/a_(b))\n- 1: (current) [new](about:blank)\n### Open tabs\npartial\n')
        cli=Mock(return_value=raw)
        for _ in range(3):
            value=json.loads(inventory(cli).stdout.split('### Result\n')[1].split('\n### ')[0])
            self.assertEqual(value['selectedIndex'],1)
            self.assertEqual(len(value['tabs']),2)
            self.assertEqual(value['tabs'][0]['url'],'https://example.com/a_(b)')
            self.assertIsNone(value['tabs'][0]['chooserOpen'])
        self.assertEqual([c.args for c in cli.call_args_list],[('tab-list',)]*3)
        for body in ('- 1: (current) [bad index](about:blank)',
                     '- 0: (current) [ambiguous](title](about:blank)',
                     '- 0: [no current](about:blank)',
                     '- 0: (current) [multi\nline](about:blank)'):
            self.assertIsNone(native_inventory(subprocess.CompletedProcess([],0,'### Result\n'+body)))
        timeout=Mock(return_value=subprocess.CompletedProcess([],124,'deadline'))
        self.assertEqual(inventory(timeout).returncode,124);self.assertEqual(timeout.call_count,1)

    def test_snapshot_refs_are_preserved_with_exactly_one_full_inventory(self):
        snap=subprocess.CompletedProcess([],0,'### Open tabs\npartial\n### Snapshot\n- textbox [ref=f10e3]\n')
        tabs=subprocess.CompletedProcess([],0,'### Result\n{}\n### Open tabs\n- 0: (current) old\n- 1: another\n')
        r=decorate_snapshot(snap,tabs)
        self.assertEqual(r.stdout.count('### Open tabs'),1);self.assertIn('[ref=f10e3]',r.stdout)
        self.assertIn('- 1: another',r.stdout);self.assertNotIn('partial',r.stdout)
        partial=decorate_snapshot(snap,subprocess.CompletedProcess([],1,'unavailable'))
        self.assertEqual(partial.returncode,0);self.assertIn('TAB_INVENTORY_UNAVAILABLE',partial.stdout)
        self.assertIn('[ref=f10e3]',partial.stdout)

    def test_visible_staged_dialog_is_prominent_without_changing_the_snapshot(self):
        body='- generic [ref=f1e1]:\n'+''.join('  - text: ordinary page content\n' for _ in range(800))+'''  - 'dialog "Step 1: Submit Documents" [ref=f1e20]':
    - heading "Step 1" [ref=f1e21]
    - button "Step 2: External Application" [ref=f1e22]
  - button "Outside" [ref=f1e30]
'''
        native=subprocess.CompletedProcess([],0,'### Page\nOriginal page\n### Snapshot\n```yaml\n'+body+'```\n')
        tabs=subprocess.CompletedProcess([],0,'### Open tabs\n- 0: (current) original\n')
        decorated=decorate_snapshot(native,tabs).stdout
        prefix=decorated.split('### Snapshot\n')[0]
        self.assertIn('### Page dialogs',prefix);self.assertIn('[ref=f1e22]',prefix)
        self.assertNotIn('Outside',prefix);self.assertIn(body,decorated)
        self.assertEqual(decorated.count('### Page dialogs'),1)
        self.assertNotIn('ordinary page content',prefix)

    def test_dialog_summary_skips_text_scalars_and_is_bounded(self):
        fake='### Snapshot\n- text: |\n    - dialog "Fake" [ref=e99]\n'
        self.assertEqual(dialog_summary(fake),'')
        self.assertEqual(dialog_summary('### Console\n- dialog "Fake" [ref=e99]'),'')
        body='### Snapshot\n- dialog "Real" [ref=e1]:\n'+''.join('  - button "Real control" [ref=e'+str(i+2)+']\n' for i in range(100))
        summary=dialog_summary(body)
        self.assertLess(len(summary),4300);self.assertIn('summary truncated',summary)
        self.assertEqual(dialog_summary('### Snapshot\n- button "No modal" [ref=e1]'),'')

    def test_modal_failure_keeps_listing_available_and_does_not_replay_selection(self):
        blocked=subprocess.CompletedProcess([],1,'### Error\nblocked by file chooser')
        rows=[{'index':0,'current':True,'title':'actual','url':'https://example.com','chooserOpen':True}]
        cli=Mock(side_effect=[subprocess.CompletedProcess([],0,'### Result\nunparseable'),blocked,subprocess.CompletedProcess([],0,'### Result\n- 0: (current) actual'),subprocess.CompletedProcess([],0,''),result({'tabs':rows,'selectedIndex':0})])
        self.assertIn('actual',inventory(cli).stdout);self.assertEqual(cli.call_args_list[2].args,('tab-list',))
        self.assertEqual(cli.call_args_list[3].args,('tab-new','about:blank'))
        cli=Mock(side_effect=[subprocess.CompletedProcess([],0,''),subprocess.CompletedProcess([],124,'deadline')])
        r=inventory(cli,2);self.assertEqual(r.returncode,124);self.assertEqual(cli.call_count,2)

    def test_bare_errors_are_informative_and_timeouts_remain_uncertain(self):
        r=describe_error('upload',subprocess.CompletedProcess([],1,'### Error\n'))
        self.assertIn('UPLOAD_NOT_CONFIRMED',r.stdout);self.assertIn('Inspect',r.stdout)
        r=describe_error('fill',subprocess.CompletedProcess([],124,''))
        self.assertEqual(r.returncode,124);self.assertIn('OUTCOME_UNCERTAIN',r.stdout)
        for data in ({'tabs':[{}]},{'tabs':'not a list'}):
            self.assertNotEqual(inventory(Mock(return_value=result(data))).returncode,0)

    def test_literal_input_cannot_become_code_and_operations_are_fixed(self):
        text="'); throw new Error('injected'); //\n leading space "
        code=script({'kind':'focused-input','ref':'e1','text':text,'operation':'type'})
        self.assertIn(json.dumps(text),code)
        with self.assertRaises(ValueError):script({'kind':'arbitrary-code','code':text})

    def test_fixed_callbacks_return_receipts_without_echoing_the_implementation(self):
        native=subprocess.CompletedProcess([],0,'### Result\n{"value":"once","readOnly":true}\n### Ran Playwright code\ninternal implementation\n')
        cli=Mock(return_value=native)
        observed=read_value(cli,'e2')
        self.assertEqual(json.loads(observed.stdout.split('\n',1)[1])['value'],'once')
        self.assertNotIn('implementation',observed.stdout)
        deadline=subprocess.CompletedProcess([],124,'uncertain')
        cli=Mock(return_value=deadline)
        self.assertIs(focus_input(cli,'type','e2','text'),deadline)
        self.assertEqual(cli.call_count,1)


if __name__=='__main__':unittest.main()

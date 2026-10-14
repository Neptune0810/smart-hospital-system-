import json,tempfile,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
import manage
class StopConfirmation(unittest.TestCase):
    def test_timeout_accepted_only_when_actual_daemon_has_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'logs').mkdir()
            result=subprocess.CompletedProcess([],1,stdout='{"running":false}',stderr='')
            with patch.object(manage,'ROOT',root),patch.object(manage.subprocess,'run',side_effect=[subprocess.TimeoutExpired('daemon stop',45),result]):
                manage.agh_control('stop')
            event=json.loads((root/'logs/agh-lifecycle.jsonl').read_text().splitlines()[0])
            self.assertEqual(event['event'],'stop_confirmed_after_cli_timeout')
    def test_nonzero_cli_result_does_not_override_verified_stopped_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'logs').mkdir()
            replies=[subprocess.CompletedProcess([],1,stdout='stopped',stderr=''),subprocess.CompletedProcess([],0,stdout='{"running":false}',stderr='')]
            with patch.object(manage,'ROOT',root),patch.object(manage.subprocess,'run',side_effect=replies):manage.agh_control('stop')
            self.assertEqual(json.loads((root/'logs/agh-lifecycle.jsonl').read_text().splitlines()[0])['cli_exit_code'],1)
    def test_timeout_must_fail_when_daemon_is_still_running(self):
        result=subprocess.CompletedProcess([],0,stdout='{"running":true,"socketReachable":true}',stderr='')
        with patch.object(manage.subprocess,'run',side_effect=[subprocess.TimeoutExpired('daemon stop',45),result]):
            with self.assertRaisesRegex(RuntimeError,'stop_unconfirmed'):manage.agh_control('stop')
if __name__=='__main__':unittest.main()

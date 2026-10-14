"""Regression checks for real AGH CLI new/resumed-session command contract."""
import tempfile,json,unittest
from pathlib import Path
from unittest.mock import patch
import agh_client
class FakeProcess:
    returncode=0
    def wait(self,timeout=None):return 0
class ResumeContract(unittest.TestCase):
    def test_new_and_resumed_session_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            commands=[]
            def launch(cmd,**kw):
                commands.append(cmd)
                kw['stdout'].write(json.dumps({'v':'agnes-cli-result/v1','exitCode':0,'sessionId':'test-session','reason':'completed','text':'test'})+'\n')
                kw['stdout'].flush()
                return FakeProcess()
            with patch.object(agh_client.subprocess,'Popen',side_effect=launch):
                agh_client.invoke('first',Path(tmp))
                agh_client.invoke('second',Path(tmp),'test-session')
            self.assertIn('--preset',commands[0]);self.assertNotIn('--resume',commands[0])
            self.assertIn('--resume',commands[1]);self.assertNotIn('--preset',commands[1])
    def test_failed_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'turn-000.log';old.write_text('failure evidence')
            def launch(cmd,**kw):
                kw['stdout'].write(json.dumps({'v':'agnes-cli-result/v1','exitCode':0,'sessionId':'test-session','text':'test'})+'\n');kw['stdout'].flush();return FakeProcess()
            with patch.object(agh_client.subprocess,'Popen',side_effect=launch):agh_client.invoke('retry',Path(tmp))
            self.assertEqual(old.read_text(),'failure evidence')
            self.assertTrue((Path(tmp)/'turn-001.json').exists())
if __name__=='__main__':unittest.main()

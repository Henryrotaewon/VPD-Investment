import tempfile
import unittest
from pathlib import Path
from magi2.fast_observe_service import Service,view
from magi2.telegram_ui import parse_command,main_keyboard
from magi1.fast_observe import Observer,now_ms

class ServiceTests(unittest.TestCase):
    def test_readonly_view_and_button(self):
        with tempfile.TemporaryDirectory() as d:
            s=Service(d,lambda x:None)
            s.save(phase='REPAIRING',done=2,total=289,complete=1,incomplete=1)
            o=Observer(str(s.db),now_ms());o.db.close()
            text=view(d)
            self.assertIn('2/289',text);self.assertIn('누적 포착 0',text)
            self.assertIn('주문 없음',text)
            self.assertEqual(parse_command('⚡ FAST 포착·추적'),'fast_watch')
            self.assertIn('⚡ FAST 포착·추적',str(main_keyboard()))
            s.stop()
    def test_not_created(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn('초기화 대기',view(d))
            self.assertFalse((Path(d)/'fast-observe').exists())

if __name__=='__main__':unittest.main()

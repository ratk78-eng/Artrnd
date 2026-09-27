import threading,time,unittest
from pathlib import Path
from unittest.mock import patch
import main

class InspectQueueTest(unittest.TestCase):
    def setUp(self):
        self.app=main.Nest();self.app.withdraw()
    def tearDown(self):self.app.destroy()
    def add(self,name):
        row={'id':len(self.app.rows)+1,'path':Path(name),'name':name,'dims':None,'length':None,'qty':1,'state':'Чтение CAD…'}
        self.app.rows.append(row);return row
    def wait(self,rows):
        deadline=time.time()+10
        while any(r['state']=='Чтение CAD…' for r in rows) and time.time()<deadline:self.app.update();time.sleep(0.01)
    def test_files_are_read_one_at_a_time(self):
        active=[0];peak=[0];lock=threading.Lock()
        def fake(path):
            with lock:active[0]+=1;peak[0]=max(peak[0],active[0])
            time.sleep(0.05)
            with lock:active[0]-=1
            return {'x':10.,'y':20.,'z':30.}
        with patch.object(main,'dimensions',fake):
            rows=[self.add(f'{i}.step') for i in range(5)]
            for r in rows:self.app.pending.put(r)
            self.wait(rows)
        self.assertEqual(peak[0],1)
        self.assertEqual([r['state'] for r in rows],['Готово']*5)
        self.assertEqual([r['length'] for r in rows],[10.]*5)
    def test_error_keeps_queue_running(self):
        def fake(path):
            if path.name=='bad.step':raise ValueError('битый файл')
            return {'x':1.,'y':2.,'z':3.}
        with patch.object(main,'dimensions',fake):
            rows=[self.add('bad.step'),self.add('good.step')]
            for r in rows:self.app.pending.put(r)
            self.wait(rows)
        self.assertEqual([r['state'] for r in rows],['Ошибка','Готово'])
    def test_removed_row_is_skipped(self):
        seen=[]
        def fake(path):seen.append(path.name);return {'x':1.,'y':2.,'z':3.}
        with patch.object(main,'dimensions',fake):
            gone=self.add('gone.step');self.app.rows.remove(gone)
            kept=self.add('kept.step')
            self.app.pending.put(gone);self.app.pending.put(kept)
            self.wait([kept])
        self.assertEqual(seen,['kept.step'])

if __name__=='__main__':unittest.main()

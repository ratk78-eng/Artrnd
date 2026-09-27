import tempfile,threading,time,unittest
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
        self.assertEqual([(r['axis'],r['length']) for r in rows],[('z',30.)]*5)
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

class RowAxisTest(unittest.TestCase):
    def setUp(self):
        self.app=main.Nest();self.app.withdraw()
        self.rows=[]
        for i,dims in enumerate([{'x':50.,'y':50.,'z':1498.},{'x':50.,'y':1498.,'z':50.},{'x':480.,'y':30.,'z':30.}],1):
            row={'id':i,'path':Path(f'{i}.step'),'name':f'{i}.step','dims':None,'axis':None,'length':None,'qty':1,'state':'Чтение CAD…'}
            self.app.rows.append(row);self.app.inspected(row,dims,None);self.rows.append(row)
    def tearDown(self):self.app.destroy()
    def test_auto_picks_longest_extent_per_row(self):
        self.assertEqual([(r['axis'],r['length']) for r in self.rows],[('z',1498.),('y',1498.),('x',480.)])
        self.assertEqual([self.app.table.set(i,'axis') for i in self.app.table.get_children()],['Z','Y','X'])
    def test_combobox_applies_to_selected_rows_only(self):
        self.app.table.selection_set(['1']);self.app.axis.set('X');self.app.axis_change()
        self.assertEqual([r['axis'] for r in self.rows],['z','x','x'])
        self.assertEqual(self.rows[1]['length'],50.)
    def test_combobox_without_selection_applies_to_all(self):
        self.app.axis.set('Z');self.app.axis_change()
        self.assertEqual([(r['axis'],r['length']) for r in self.rows],[('z',1498.),('z',50.),('z',30.)])
    def test_new_rows_use_fixed_axis(self):
        self.app.axis.set('Y')
        row={'id':9,'path':Path('9.step'),'name':'9.step','dims':None,'axis':None,'length':None,'qty':1,'state':'Чтение CAD…'}
        self.app.rows.append(row);self.app.inspected(row,{'x':1.,'y':2.,'z':3.},None)
        self.assertEqual((row['axis'],row['length']),('y',2.))
    def test_cycle_axis(self):
        row=self.rows[2]
        self.app.cycle_axis(row);self.assertEqual((row['axis'],row['length']),('y',30.))
        self.app.cycle_axis(row);self.app.cycle_axis(row);self.assertEqual(row['axis'],'x')
    def test_axis_reaches_plan_and_labels(self):
        self.app.vars['stock'].set('6000');self.app.calculate()
        parts=[p for j in self.app.jobs for p in j['parts']]
        self.assertEqual(sorted(p['axis'] for p in parts),['x','y','z'])
        rows=[self.app.result.item(i) for g in self.app.result.get_children() for i in self.app.result.get_children(g)]
        self.assertIn(('1','Z','1498'),[(r['text'],*map(str,r['values'][:2])) for r in rows])

class ResultViewTest(unittest.TestCase):
    V={'stock':6000.,'kerf':3.,'add':0.,'maxwaste':150.,'overrun':500.}
    def setUp(self):self.app=main.Nest();self.app.withdraw()
    def tearDown(self):self.app.destroy()
    def show(self,rows):
        jobs=main.plan(rows,self.V['stock'],self.V['kerf'],self.V['add']);self.app.show_result(jobs,self.V);self.app.update();return jobs
    def test_ranges(self):
        self.assertEqual(main.ranges([1,2,3,5,7,8]),'1–3, 5, 7–8');self.assertEqual(main.ranges([4]),'4')
    def test_identical_stocks_are_grouped_and_parts_counted(self):
        # 1498 ×4 fits one stock (5992+3*3=6001 > 6000 → 3 per stock): 12 tubes → 4 identical stocks
        jobs=self.show([{'id':1,'name':'Труба 50.stp','length':1498.,'qty':12,'axis':'z'}])
        tree=self.app.result;groups=tree.get_children()
        self.assertEqual(len(jobs),4);self.assertEqual(len(groups),1)
        self.assertEqual(tree.item(groups[0])['text'],'Хлысты 1–4  ×4')
        child=tree.item(tree.get_children(groups[0])[0])
        self.assertEqual((child['text'],*map(str,child['values'][:3])),('Труба 50','Z','1498','3'))
    def test_summary_and_status_colours(self):
        self.show([{'id':1,'name':'a','length':5000.,'qty':1,'axis':'x'},{'id':2,'name':'b','length':5800.,'qty':1,'axis':'x'},{'id':3,'name':'c','length':5900.,'qty':1,'axis':'x'}])
        summary=self.app.summary.get()
        self.assertIn('Хлыстов: 3',summary);self.assertIn('Перерасход: 1',summary);self.assertIn('Выше макс. отхода: 1',summary)
        tags=[self.app.result.item(g)['tags'][1] for g in self.app.result.get_children()]
        self.assertEqual(sorted(tags),['ok','over','warn'])
    def test_bar_is_drawn_and_follows_selection(self):
        self.show([{'id':1,'name':'a','length':2000.,'qty':2,'axis':'x'},{'id':2,'name':'b','length':5000.,'qty':1,'axis':'x'}])
        self.assertTrue(self.app.bar.find_all())
        texts=lambda:[self.app.bar.itemcget(i,'text') for i in self.app.bar.find_all() if self.app.bar.type(i)=='text']
        first=texts();self.app.result.selection_set('g1');self.app.update()
        self.assertNotEqual(first,texts())
    def test_changed_clears_result(self):
        self.show([{'id':1,'name':'a','length':1000.,'qty':1,'axis':'x'}])
        self.app.changed()
        self.assertEqual(self.app.result.get_children(),());self.assertEqual(self.app.bar.find_all(),())

class ExportFolderTest(unittest.TestCase):
    def setUp(self):
        self.app=main.Nest();self.app.withdraw()
        self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self):self.app.destroy();self.tmp.cleanup()
    def test_creates_missing_parent_folders(self):
        # installed build points ROOT at %LOCALAPPDATA%\NEST, which may not exist yet
        root=Path(self.tmp.name)/'LocalAppData'/'NEST'
        done=[]
        v={'stock':1000.,'kerf':3.,'add':0.,'maxwaste':150.,'overrun':150.}
        with patch.object(main,'ROOT',root),patch.object(main,'write_step',lambda *a:['stock_001.step']),\
             patch.object(self.app,'export_done',lambda *a:done.append(a)),patch.object(main.messagebox,'showerror'):
            self.app.export_worker([],(0,v,{}))
            deadline=time.time()+5
            while not done and time.time()<deadline:self.app.update();time.sleep(0.01)
        snapshot,folder,result,error=done[0]
        self.assertIsNone(error);self.assertEqual(folder.parent,root/'exports')
        self.assertTrue((folder/'manifest.json').is_file())

if __name__=='__main__':unittest.main()

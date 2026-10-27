import copy,json,os,re,tempfile,unittest
from datetime import datetime
from lathe_cam.machine import Machine,load
from lathe_cam.contour import Contour
from lathe_cam.cam import Job
from lathe_cam.post_808d import Post,fmt,ascii_text
from lathe_cam.preview import svg
from lathe_cam.__main__ import build

HERE=os.path.dirname(__file__)
with open(os.path.join(HERE,'examples','val.json'),encoding='utf-8') as f:EXAMPLE=json.load(f)
NOW=datetime(2026,1,1)

def job(**changes):
    d=copy.deepcopy(EXAMPLE);d.update(changes);return Job(d,Machine())
def program(j,**kw):return Post(j.machine,now=NOW,**kw).run(j)

class TestContour(unittest.TestCase):
    def test_arc_points_stay_on_circle(self):
        # Z to the right, X up: G2 from (r20,z-55) to (r25,z-60) is the inner fillet, centre (r25,z-55);
        # G3 between the same points is the rounded edge, centre (r20,z-60)
        for d,(cr,cz) in (('cw',(25,-55)),('ccw',(20,-60))):
            pts=Contour({'x':40,'z':-55},[{'type':'arc','x':50,'z':-60,'r':5,'dir':d}]).points(0.1)
            self.assertEqual(pts[0],(40,-55));self.assertAlmostEqual(pts[-1][0],50);self.assertAlmostEqual(pts[-1][1],-60)
            self.assertGreater(len(pts),10)
            for x,z in pts:self.assertAlmostEqual(((x/2-cr)**2+(z-cz)**2)**.5,5,places=6)
    def test_bad_arc(self):
        with self.assertRaises(ValueError):Contour({'x':0,'z':0},[{'type':'arc','x':40,'z':-30,'r':5}])
    def test_zero_and_negative(self):
        with self.assertRaises(ValueError):Contour({'x':10,'z':0},[{'x':10,'z':0}])
        with self.assertRaises(ValueError):Contour({'x':10,'z':0},[{'x':-1,'z':-5}])
        with self.assertRaises(ValueError):Contour({'x':10,'z':0},[])
    def test_diameter_at(self):
        c=job().contour
        self.assertEqual(c.diameter_at(-10),30);self.assertEqual(c.diameter_at(-26,-29.95),30)
        self.assertEqual(c.diameter_at(-26,-31),40);self.assertIsNone(c.diameter_at(-100))
        self.assertAlmostEqual(c.diameter_at(-1),28)

class TestJob(unittest.TestCase):
    def test_limits(self):
        with self.assertRaises(ValueError):job(stock={'diameter':500,'length':100})
        with self.assertRaises(ValueError):job(stock={'diameter':60,'length':5000})
        with self.assertRaises(ValueError):job(stock={'diameter':40,'length':110})   # contour Ø56
        with self.assertRaises(ValueError):job(program='1BAD')
        with self.assertRaises(ValueError):job(program='ВАЛ')
    def test_tool_and_rpm_checks(self):
        d=copy.deepcopy(EXAMPLE);d['tools']['9']={'name':'x'}
        with self.assertRaises(ValueError):Job(d,Machine())
        d=copy.deepcopy(EXAMPLE);d['operations'][0]['lims']=5000
        with self.assertRaises(ValueError):Job(d,Machine()).moves()
        d=copy.deepcopy(EXAMPLE);del d['operations'][4]['rpm'];d['operations'][4]['vc']=100
        with self.assertRaisesRegex(ValueError,'G97'):Job(d,Machine()).moves()
        d=copy.deepcopy(EXAMPLE);d['operations'][0]['tool']=4;del d['tools']['4']
        with self.assertRaises(ValueError):Job(d,Machine()).moves()
    def test_unknown_operation(self):
        with self.assertRaises(ValueError):job(operations=[{'type':'mill','tool':1,'vc':1}]).moves()
    def test_face_passes(self):
        mv=job(operations=[EXAMPLE['operations'][0]]).moves()
        cuts=[m for m in mv if m[0]=='feed']
        self.assertEqual([(m[1],m[2]) for m in cuts],[(-1.6,1.),(-1.6,0.)])
    def test_groove_starts_at_turned_surface(self):
        mv=job(operations=[EXAMPLE['operations'][3]]).moves()
        feeds=[m for m in mv if m[0]=='feed']
        self.assertLessEqual(max(m[1] for m in feeds),30)
        self.assertEqual({m[2] for m in feeds},{-26.,-27.})
        self.assertEqual(min(m[1] for m in feeds),26)
    def test_groove_too_narrow(self):
        op=dict(EXAMPLE['operations'][3],width=2)
        with self.assertRaises(ValueError):job(operations=[op]).moves()
    def test_drill_pecks(self):
        d=copy.deepcopy(EXAMPLE);d['tools']['4']={'name':'SVERLO 10'}
        d['operations']=[{'type':'drill','tool':4,'rpm':800,'feed':0.1,'depth':12,'peck':5}]
        mv=Job(d,Machine()).moves()
        self.assertEqual([m[2] for m in mv if m[0]=='feed'],[-3.,-8.,-12.])
        self.assertTrue(all(m[1]==0 for m in mv if m[0] in ('feed','rapid')))

class TestPost(unittest.TestCase):
    def setUp(self):self.files=program(job())
    def test_files(self):self.assertEqual(sorted(self.files),['VAL_01.MPF','VAL_01_K.SPF'])
    def test_header_and_end(self):
        lines=self.files['VAL_01.MPF'].splitlines()
        self.assertIn('G18 G90 G95 G71 G40 DIAMON',lines);self.assertIn('G54',lines)
        self.assertEqual(lines[-2:],['M5','M30'])
    def test_cycles(self):
        text=self.files['VAL_01.MPF']
        self.assertIn('CYCLE95("VAL_01_K",2,0.1,0.4,0,0.25,0.1,0.25,1,,)',text)
        self.assertIn('CYCLE95("VAL_01_K",0,0,0,0,0,0.1,0.1,5,,)',text)
        self.assertIn('CYCLE97(1.5,,0,-24,30,30,3,1.5,0.92,0.05,30,0,8,1,1,1)',text)
        self.assertIn('G96 S180 LIMS=2000 M3',text);self.assertIn('G97 S600 M3',text)
    def test_tool_change(self):
        text=self.files['VAL_01.MPF']
        self.assertEqual(re.findall(r'^T(\d) D1$',text,re.M),['1','2','3','4','3'])
    def test_subprogram(self):
        spf=self.files['VAL_01_K.SPF'].splitlines()
        self.assertEqual(spf[2],'G1 X26 Z0');self.assertIn('G2 X50 Z-60 CR=5',spf);self.assertEqual(spf[-1],'M17')
    def test_labels_mode(self):
        files=program(job(),contour_mode='labels')
        self.assertEqual(list(files),['VAL_01.MPF'])
        text=files['VAL_01.MPF']
        self.assertIn('CYCLE95("VAL_01_K_S:VAL_01_K_E"',text)
        self.assertLess(text.index('M30'),text.index('VAL_01_K_S:\n'))
    def test_vrt_and_line_numbers(self):
        j=job();j.machine.line_numbers=True
        text=program(j,vrt=True)['VAL_01.MPF']
        self.assertRegex(text,r'CYCLE95\(.*,1,,,1\)');self.assertTrue(re.search(r'^N10 G18',text,re.M))
    def test_ascii_only(self):
        for text in self.files.values():text.encode('ascii')
    def test_modal_words(self):
        # no block repeats the position it is already at, and F is only written when it changes
        for line in self.files['VAL_01.MPF'].splitlines():self.assertNotRegex(line,r'^G[01]$')
        self.assertEqual(self.files['VAL_01.MPF'].count('F0.2\n'),1)
    def test_fmt(self):
        self.assertEqual([fmt(v) for v in (1.,-0.0001,0.25,12.3456,-1.6)],['1','0','0.25','12.346','-1.6'])
        self.assertEqual(ascii_text('ЗАГОТОВКА Ø60'),'ZAGOTOVKA D60')

class TestCli(unittest.TestCase):
    def test_build_and_preview(self):
        with tempfile.TemporaryDirectory() as d:
            files=build(os.path.join(HERE,'examples','val.json'),d)
            self.assertEqual(sorted(os.path.basename(f) for f in files),['VAL_01.MPF','VAL_01.svg','VAL_01_K.SPF'])
            with open(os.path.join(d,'VAL_01.MPF'),'rb') as f:self.assertIn(b'\r\nM30\r\n',f.read())
        self.assertIn('<polyline',svg(job()))
    def test_machine_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,'m.json')
            with open(p,'w') as f:json.dump({'max_rpm':3000,'tool_positions':8},f)
            self.assertEqual(load(p).max_rpm,3000)
            with open(p,'w') as f:json.dump({'rpm':3000},f)
            with self.assertRaises(ValueError):load(p)

if __name__=='__main__':unittest.main()

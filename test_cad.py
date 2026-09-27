import tempfile,unittest
from pathlib import Path
import cad
try:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakeCone,BRepPrimAPI_MakeBox
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs,STEPControl_Reader
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.gp import gp_Pnt
    HAVE_OCP=True
except ImportError:HAVE_OCP=False

def pipe(outer,inner,length):
    return BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(outer,length).Shape(),BRepPrimAPI_MakeCylinder(inner,length).Shape()).Shape()

def save(shape,path):
    writer=STEPControl_Writer();writer.Transfer(shape,STEPControl_AsIs);writer.Write(str(path))
    return path

def solids(shape):
    it=TopExp_Explorer(shape,TopAbs_SOLID);n=0
    while it.More():n+=1;it.Next()
    return n

@unittest.skipUnless(HAVE_OCP,'OCP не установлен')
class CadTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_pipe_dimensions(self):
        d=cad.dimensions(save(pipe(25,23,1498),self.dir/'pipe.step'))
        self.assertAlmostEqual(d['z'],1498,delta=0.01)
        self.assertAlmostEqual(d['x'],50,delta=0.1);self.assertAlmostEqual(d['y'],50,delta=0.1)
    def test_multiple_solids_rejected(self):
        compound=TopoDS_Compound();builder=BRep_Builder();builder.MakeCompound(compound)
        builder.Add(compound,BRepPrimAPI_MakeBox(10,10,10).Shape());builder.Add(compound,BRepPrimAPI_MakeBox(gp_Pnt(20,0,0),10,10,10).Shape())
        with self.assertRaisesRegex(ValueError,'одно твёрдое тело'):cad.load(save(compound,self.dir/'two.step'))
    def test_unreadable_file(self):
        path=self.dir/'bad.step';path.write_text('not a step',encoding='utf8')
        with self.assertRaises(ValueError):cad.load(path)
    def test_prolong_flat_end(self):
        shape=cad.prolong(pipe(25,23,1000),'z',5,'pipe')
        self.assertEqual(solids(shape),1)
        lo,hi=cad.extent(shape,'z');self.assertAlmostEqual(lo,0,delta=0.01);self.assertAlmostEqual(hi,1005,delta=0.01)
    def test_prolong_zero_returns_original(self):
        shape=pipe(25,23,1000);self.assertIs(cad.prolong(shape,'z',0,'pipe'),shape)
    def test_prolong_needs_flat_end(self):
        cone=BRepPrimAPI_MakeCone(10,0,100).Shape()
        with self.assertRaisesRegex(ValueError,'плоский конечный торец'):cad.prolong(cone,'z',5,'cone')
    def test_write_step_assembly(self):
        sources={1:save(pipe(25,23,1000),self.dir/'a.step'),2:save(pipe(15,13,500),self.dir/'b.step')}
        stock,kerf,add=2000,3,2
        used=1000+add+kerf+500+add
        jobs=[{'number':1,'parts':[{'source':1,'length':1000.0,'name':'a'},{'source':2,'length':500.0,'name':'b'}],'waste':stock-used}]
        out=self.dir/'out';out.mkdir()
        self.assertEqual(cad.write_step(jobs,sources,'z',stock,kerf,add,out),['stock_001.step'])
        reader=STEPControl_Reader();reader.ReadFile(str(out/'stock_001.step'));reader.TransferRoots();result=reader.OneShape()
        self.assertEqual(solids(result),2)
        lo,hi=cad.extent(result,'z');self.assertAlmostEqual(lo,0,delta=0.2);self.assertAlmostEqual(hi,used,delta=0.2)
    def test_write_step_length_mismatch(self):
        sources={1:save(pipe(25,23,1000),self.dir/'a.step')}
        jobs=[{'number':1,'parts':[{'source':1,'length':900.0,'name':'a'}],'waste':1100}]
        with self.assertRaisesRegex(ValueError,'Длина CAD отличается'):cad.write_step(jobs,sources,'z',2000,3,0,self.dir)

if __name__=='__main__':unittest.main()

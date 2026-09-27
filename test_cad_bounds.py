import sys,types,unittest
from unittest.mock import patch
import cad
class Point:
    def __init__(self,x,y,z):self.v=x,y,z
    def X(self):return self.v[0]
    def Y(self):return self.v[1]
    def Z(self):return self.v[2]
class Box:
    def __init__(self):self.minimum=Point(10,20,30);self.maximum=Point(40,60,90)
    def IsVoid(self):return False
    def CornerMin(self):return self.minimum
    def CornerMax(self):return self.maximum
class Lib:
    @staticmethod
    def Add_s(shape,box):pass
class BoundsTest(unittest.TestCase):
    def test_all_axes_without_get(self):
        with patch.dict(sys.modules,{'OCP':types.ModuleType('OCP'),'OCP.Bnd':types.SimpleNamespace(Bnd_Box=Box),'OCP.BRepBndLib':types.SimpleNamespace(BRepBndLib=Lib)}):
            self.assertEqual(cad.extent(object(),'x'),(10,40))
            self.assertEqual(cad.extent(object(),'y'),(20,60))
            self.assertEqual(cad.extent(object(),'z'),(30,90))
if __name__=='__main__':unittest.main()

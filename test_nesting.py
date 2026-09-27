import unittest
from nesting import plan
class TestNesting(unittest.TestCase):
    def test_kerf_allowance(self):
        j=plan([{'id':1,'name':'a','length':498,'qty':1},{'id':2,'name':'b','length':499,'qty':1}],1000,3,0)
        self.assertEqual(len(j),1);self.assertAlmostEqual(j[0]['waste'],0)
    def test_allowance(self):
        j=plan([{'id':1,'name':'a','length':400,'qty':2}],1000,2,5)
        self.assertAlmostEqual(j[0]['waste'],188)
    def test_single_no_last_cut(self):self.assertEqual(plan([{'id':1,'name':'a','length':100,'qty':1}],1000,3,0)[0]['waste'],900)
    def test_too_long(self):
        with self.assertRaises(ValueError):plan([{'id':1,'name':'a','length':1001,'qty':1}],1000,3,0)
    def test_missing(self):
        with self.assertRaises(ValueError):plan([],1000,3,0)
if __name__=='__main__':unittest.main()

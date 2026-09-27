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
    def test_too_long_with_allowance(self):
        with self.assertRaises(ValueError):plan([{'id':1,'name':'a','length':998,'qty':1}],1000,3,5)
    def test_bad_quantity(self):
        for qty in (0,-1,1.5):
            with self.assertRaises(ValueError):plan([{'id':1,'name':'a','length':100,'qty':qty}],1000,3,0)
    def test_bad_parameters(self):
        for stock,kerf,add in ((0,3,0),(1000,-1,0),(1000,3,-1),(float('nan'),3,0)):
            with self.assertRaises(ValueError):plan([{'id':1,'name':'a','length':100,'qty':1}],stock,kerf,add)
    def test_part_limit(self):
        with self.assertRaises(ValueError):plan([{'id':1,'name':'a','length':1,'qty':10001}],1000,3,0)
    def test_best_fit_fills_tightest_stock(self):
        # 600 и 500 открывают два хлыста; 400 влезает только в хлыст с 500 (остаток 97)
        j=plan([{'id':1,'name':'a','length':600,'qty':1},{'id':2,'name':'b','length':500,'qty':1},{'id':3,'name':'c','length':400,'qty':1}],1000,3,0)
        self.assertEqual([[p['length'] for p in x['parts']] for x in j],[[600],[500,400]])
    def test_balance_and_order(self):
        rows=[{'id':1,'name':'a','length':1768,'qty':4},{'id':2,'name':'b','length':1263,'qty':24},{'id':3,'name':'c','length':1563,'qty':6}]
        jobs=plan(rows,6000,3,2)
        self.assertEqual([x['number'] for x in jobs],list(range(1,len(jobs)+1)))
        self.assertEqual(sum(len(x['parts']) for x in jobs),34)
        for x in jobs:
            used=sum(p['length']+2 for p in x['parts'])+3*(len(x['parts'])-1)
            self.assertGreaterEqual(x['waste'],0);self.assertAlmostEqual(x['waste'],6000-used)
if __name__=='__main__':unittest.main()

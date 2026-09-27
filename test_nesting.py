import random,time,unittest
from collections import Counter
from nesting import plan,lower_bound,_best_fit,_score
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
class TestSearch(unittest.TestCase):
    ROWS=[{'id':1,'name':'a','length':1600,'qty':4},{'id':2,'name':'b','length':2700,'qty':2}]
    def test_best_fit_alone_needs_three_stocks(self):
        parts=sorted(({'length':r['length']} for r in self.ROWS for _ in range(r['qty'])),key=lambda p:-p['length'])
        self.assertEqual(len(_best_fit(parts,6000,3,0)),3)
    def test_search_finds_two_stocks(self):
        # both 2700 in one stock (best fit) leaves the four 1600 for two more stocks; 2700+1600+1600 twice is enough
        jobs=plan(self.ROWS,6000,3,0)
        self.assertEqual([[p['length'] for p in j['parts']] for j in jobs],[[2700,1600,1600]]*2)
        self.assertEqual([j['waste'] for j in jobs],[94,94])
        self.assertEqual(len(jobs),lower_bound(self.ROWS,6000,3,0))
    def test_lower_bound(self):
        self.assertEqual(lower_bound(self.ROWS,6000,3,0),2)
        # 34 parts of the built-in example need at least 8 stocks by length (9 is the real optimum)
        self.assertEqual(lower_bound([{'length':1768,'qty':4},{'length':1263,'qty':24},{'length':1563,'qty':6}],6000,3,0),8)
    def test_score_prefers_fewer_stocks_then_one_long_offcut(self):
        spread=[[{'length':5000}],[{'length':5000}]];gathered=[[{'length':5900}],[{'length':4100}]]
        self.assertLess(_score(gathered,6000,3,0),_score(spread,6000,3,0))
        self.assertLess(_score([[{'length':100}]],6000,3,0),_score(spread,6000,3,0))
    def test_never_worse_than_best_fit_and_keeps_every_part(self):
        rng=random.Random(3)
        for _ in range(40):
            rows=[{'id':i,'name':str(i),'length':float(rng.randint(250,2900)),'qty':rng.randint(1,12)} for i in range(rng.randint(2,9))]
            parts=sorted(({'length':r['length']} for r in rows for _ in range(r['qty'])),key=lambda p:-p['length'])
            jobs=plan(rows,6000,3,2)
            self.assertLessEqual(len(jobs),len(_best_fit(parts,6000,3,2)))
            self.assertGreaterEqual(len(jobs),lower_bound(rows,6000,3,2))
            self.assertEqual(Counter(p['source'] for j in jobs for p in j['parts']),Counter({r['id']:r['qty'] for r in rows}))
            for j in jobs:
                used=sum(p['length']+2 for p in j['parts'])+3*(len(j['parts'])-1)
                self.assertGreaterEqual(j['waste'],-1e-6);self.assertAlmostEqual(j['waste'],6000-used)
    def test_largest_order_stays_fast(self):
        rng=random.Random(5)
        rows=[{'id':i,'name':str(i),'length':rng.randint(20000,300000)/100,'qty':50} for i in range(200)]
        start=time.time();plan(rows,6000,3,0)
        self.assertLess(time.time()-start,15)

if __name__=='__main__':unittest.main()

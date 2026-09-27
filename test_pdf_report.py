import tempfile,unittest
from pathlib import Path
from pdf_report import write_report
try:
    import reportlab
    HAVE_REPORTLAB=True
except ImportError:HAVE_REPORTLAB=False

@unittest.skipUnless(HAVE_REPORTLAB,'reportlab не установлен')
class PdfReportTest(unittest.TestCase):
    def test_report_with_per_part_axes(self):
        values={'stock':6000.,'kerf':3.,'add':2.,'maxwaste':150.,'overrun':150.}
        jobs=[{'number':1,'parts':[{'source':1,'length':1498.,'name':'Труба 50 мм','axis':'z'},{'source':2,'length':480.,'name':'Пример','axis':None}],'waste':4015.}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'report.pdf'
            write_report(path,jobs,values)
            self.assertTrue(path.read_bytes().startswith(b'%PDF'))

if __name__=='__main__':unittest.main()

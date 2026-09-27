from pathlib import Path
import ast
p=Path(__file__).resolve().parent/'main.py'
if not p.is_file():raise SystemExit('Не найден main.py рядом с патчем.')
s=p.read_text(encoding='utf8')
if 'command=self.save_pdf' in s:raise SystemExit('PDF уже добавлен; повторять не нужно.')
changes=[
("self.csv=ttk.Button(row,text='CSV',command=self.save_csv,state='disabled');self.csv.pack(side='left',padx=3)",
 "self.csv=ttk.Button(row,text='CSV',command=self.save_csv,state='disabled');self.csv.pack(side='left',padx=3)\n        self.pdf=ttk.Button(row,text='Отчёт PDF',command=self.save_pdf,state='disabled');self.pdf.pack(side='left',padx=3)"),
("self.csv.configure(state='disabled');self.text.delete('1.0','end')",
 "self.csv.configure(state='disabled');self.pdf.configure(state='disabled');self.text.delete('1.0','end')"),
("self.csv.configure(state='normal');self.step.configure(",
 "self.csv.configure(state='normal');self.pdf.configure(state='normal');self.step.configure(")]
for old,new in changes:
    count=s.count(old)
    if count!=1:raise SystemExit(f'Ожидалось одно совпадение, найдено {count}: {old[:65]}')
    s=s.replace(old,new,1)
marker="if __name__=='__main__':Nest().mainloop()"
if s.count(marker)!=1:raise SystemExit('Версия main.py отличается. Остановлено.')
s=s.replace(marker,"    def save_pdf(self):\n        if not self.jobs:return\n        filename=filedialog.asksaveasfilename(parent=self,defaultextension='.pdf',filetypes=[('PDF','*.pdf')],initialfile='NEST_report.pdf')\n        if not filename:return\n        try:\n            from pdf_report import write_report\n            write_report(filename,self.jobs,self.snapshot[1],self.snapshot[2])\n        except ImportError:\n            messagebox.showerror('Отчёт PDF','Не найден reportlab. Установите: C:/Python/python.exe -m pip install --user reportlab',parent=self)\n        except Exception as exc:\n            messagebox.showerror('Ошибка PDF',str(exc),parent=self)\n        else:self.status.set('Сохранён отчёт PDF: '+filename)\n"+'\n'+marker)
ast.parse(s)
backup=p.with_name('main.py.before_pdf')
if backup.exists():raise SystemExit('Резервная копия main.py.before_pdf уже существует; остановлено.')
backup.write_text(p.read_text(encoding='utf8'),encoding='utf8')
p.write_text(s,encoding='utf8')
print('Добавлена кнопка «Отчёт PDF». Резервная копия: main.py.before_pdf')

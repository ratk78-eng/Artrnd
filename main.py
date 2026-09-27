import csv,datetime,json,math,os,threading,tkinter as tk
from pathlib import Path
from tkinter import ttk,filedialog,messagebox,simpledialog
from nesting import plan
from cad import dimensions,write_step
ROOT=Path(__file__).resolve().parent

class Nest(tk.Tk):
    def __init__(self):
        super().__init__();self.title('NEST — нестинг труб');self.geometry('1050x720')
        self.rows=[];self.jobs=None;self.folder=None;self.generation=0;self.busy=False
        self.vars={key:tk.StringVar(value=default) for key,default in [('stock','6000'),('kerf','3'),('add','0'),('maxwaste','150'),('overrun','150')]}
        self.axis=tk.StringVar(value='X');self.status=tk.StringVar(value='Готово к загрузке STEP/IGES.')
        self.build()
    def build(self):
        outer=ttk.Frame(self,padding=10);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='NEST — раскладка труб',font=('Segoe UI',17,'bold')).pack(anchor='w')
        settings=ttk.LabelFrame(outer,text='Параметры',padding=8);settings.pack(fill='x',pady=8)
        for i,(key,title) in enumerate([('stock','Хлыст, мм'),('kerf','Рез, мм'),('add','Припуск, мм'),('maxwaste','Макс. отход, мм'),('overrun','Перерасход, мм')]):
            ttk.Label(settings,text=title).grid(row=0,column=i,sticky='w',padx=5)
            e=ttk.Entry(settings,width=16,textvariable=self.vars[key]);e.grid(row=1,column=i,padx=5)
            e.bind('<KeyRelease>',lambda event:self.changed())
        ttk.Label(settings,text='Ось').grid(row=0,column=5)
        box=ttk.Combobox(settings,textvariable=self.axis,values=['X','Y','Z'],state='readonly',width=6);box.grid(row=1,column=5)
        box.bind('<<ComboboxSelected>>',self.axis_change)
        group=ttk.LabelFrame(outer,text='Детали — двойной щелчок меняет количество',padding=7);group.pack(fill='both',expand=True)
        columns=('file','x','y','z','length','qty','state')
        self.table=ttk.Treeview(group,columns=columns,show='headings',selectmode='extended',height=9)
        for col,title,width in [('file','CAD-файл',235),('x','X, мм',85),('y','Y, мм',85),('z','Z, мм',85),('length','Длина, мм',100),('qty','Кол-во',70),('state','Статус',220)]:
            self.table.heading(col,text=title);self.table.column(col,width=width,anchor='w' if col in ('file','state') else 'center')
        self.table.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(group,command=self.table.yview);scroll.pack(side='right',fill='y');self.table.configure(yscrollcommand=scroll.set)
        self.table.bind('<Double-1>',self.edit_quantity)
        row=ttk.Frame(outer);row.pack(fill='x',pady=5)
        for label,command in [('Добавить STEP/IGES',self.add_files),('Удалить строки',self.remove),('Пример',self.example),('Очистить',self.clear)]:ttk.Button(row,text=label,command=command).pack(side='left',padx=3)
        row=ttk.Frame(outer);row.pack(fill='x',pady=5)
        ttk.Button(row,text='Рассчитать',command=self.calculate).pack(side='left',padx=3)
        self.step=ttk.Button(row,text='Создать STEP',command=self.export,state='disabled');self.step.pack(side='left',padx=3)
        self.open=ttk.Button(row,text='Открыть папку',command=self.open_folder,state='disabled');self.open.pack(side='left',padx=3)
        self.csv=ttk.Button(row,text='CSV',command=self.save_csv,state='disabled');self.csv.pack(side='left',padx=3)
        self.pdf=ttk.Button(row,text='Отчёт PDF',command=self.save_pdf,state='disabled');self.pdf.pack(side='left',padx=3)
        result=ttk.LabelFrame(outer,text='Результат',padding=7);result.pack(fill='both',expand=True)
        self.text=tk.Text(result,height=11,wrap='word',font=('Consolas',10));self.text.pack(fill='both',expand=True)
        ttk.Label(outer,textvariable=self.status,wraplength=990).pack(fill='x',pady=4)
    def changed(self):
        self.generation+=1;self.jobs=None;self.folder=None
        self.step.configure(state='disabled');self.open.configure(state='disabled');self.csv.configure(state='disabled');self.pdf.configure(state='disabled');self.text.delete('1.0','end')
    def refresh(self):
        self.table.delete(*self.table.get_children())
        for n,r in enumerate(self.rows):
            dims=r.get('dims') or {}
            self.table.insert('', 'end',iid=str(n),values=(r['name'],*(dims.get(a,'') for a in 'xyz'),r.get('length',''),r['qty'],r['state']))
    def axis_change(self,event=None):
        self.changed()
        for r in self.rows:
            if r.get('dims'):r['length']=r['dims'][self.axis.get().lower()]
        self.refresh();self.status.set('Ось изменена. Рассчитайте раскладку заново.')
    def add_files(self):
        names=filedialog.askopenfilenames(parent=self,filetypes=[('STEP / IGES','*.stp *.step *.igs *.iges')])
        if not names:return
        if self.rows and all(r['path'] is None for r in self.rows):self.rows.clear()
        self.changed()
        for filename in names:
            p=Path(filename)
            if p.suffix.lower() not in ('.stp','.step','.igs','.iges'):continue
            row={'id':max([r['id'] for r in self.rows],default=0)+1,'path':p,'name':p.name,'dims':None,'length':None,'qty':1,'state':'Чтение CAD…'}
            self.rows.append(row);threading.Thread(target=self.inspect_worker,args=(row,),daemon=True).start()
        self.refresh();self.status.set('Определяются размеры X/Y/Z…')
    def inspect_worker(self,row):
        try:dims=dimensions(row['path']);error=None
        except Exception as e:dims=None;error=f'{type(e).__name__}: {e}'
        try:self.after(0,lambda:self.inspected(row,dims,error))
        except RuntimeError:pass
    def inspected(self,row,dims,error):
        if not any(x is row for x in self.rows):return
        if error:row['state']='Ошибка';self.status.set(row['name']+': '+error)
        else:row['dims']=dims;row['length']=dims[self.axis.get().lower()];row['state']='Готово';self.status.set('Размеры загружены. Проверьте ось и количество.')
        self.refresh()
    def edit_quantity(self,event):
        item=self.table.identify_row(event.y)
        if not item:return
        row=self.rows[int(item)]
        value=simpledialog.askinteger('Количество','Количество деталей',parent=self,initialvalue=row['qty'],minvalue=1,maxvalue=10000)
        if value is not None:row['qty']=value;self.changed();self.refresh()
    def remove(self):
        for i in sorted((int(i) for i in self.table.selection()),reverse=True):self.rows.pop(i)
        self.changed();self.refresh()
    def clear(self):self.rows.clear();self.changed();self.refresh();self.status.set('Очищено')
    def example(self):
        self.rows=[{'id':i,'path':None,'name':'Пример '+str(length),'dims':None,'length':length,'qty':qty,'state':'Только расчёт'} for i,(length,qty) in enumerate([(1768,4),(1263,24),(1563,6)],1)]
        self.changed();self.refresh();self.status.set('Пример не экспортируется в STEP.')
    def values(self):
        try:v={key:float(variable.get().replace(',','.')) for key,variable in self.vars.items()}
        except ValueError:raise ValueError('Параметры должны быть числами')
        if not all(math.isfinite(x) for x in v.values()) or v['stock']<=0 or min(v['kerf'],v['add'],v['maxwaste'],v['overrun'])<0:raise ValueError('Проверьте параметры')
        return v
    def calculate(self):
        try:
            v=self.values()
            for r in self.rows:
                if r['path'] is not None and r.get('dims') is None:raise ValueError('CAD ещё не прочитан: '+r['name'])
            self.jobs=plan(self.rows,v['stock'],v['kerf'],v['add'])
            self.snapshot=(self.generation,v,self.axis.get().lower(),{r['id']:r['path'] for r in self.rows if r['path']})
            self.text.delete('1.0','end')
            self.text.insert('end',f'Хлыстов: {len(self.jobs)}; деталей: {sum(len(j["parts"]) for j in self.jobs)}; отход: {sum(j["waste"] for j in self.jobs):.2f} мм\n')
            for j in self.jobs:
                w=j['waste'];flag='ПЕРЕРАСХОД' if w>=v['overrun'] else 'ВЫШЕ МАКС. ОТХОДА' if w>v['maxwaste'] else 'OK'
                self.text.insert('end',f"{j['number']}: "+' + '.join(p['name']+' '+str(p['length']) for p in j['parts'])+f' | отход {w:.2f} мм | {flag}\n')
            self.csv.configure(state='normal');self.pdf.configure(state='normal');self.step.configure(state='normal' if all(r['path'] for r in self.rows) else 'disabled')
            self.status.set('Расчёт готов. Пороги отхода только отмечаются, не гарантируются алгоритмом.')
        except Exception as exc:self.changed();messagebox.showerror('Ошибка расчёта',str(exc),parent=self)
    def export(self):
        if not self.jobs or self.busy or self.snapshot[0]!=self.generation:return
        self.busy=True;self.step.configure(state='disabled');self.open.configure(state='disabled');self.status.set('Создаю STEP…')
        threading.Thread(target=self.export_worker,args=(self.jobs,self.snapshot),daemon=True).start()
    def export_worker(self,jobs,snapshot):
        folder=None
        try:
            _,v,axis,sources=snapshot;root=ROOT/'exports';root.mkdir(exist_ok=True)
            base=datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S');name=base;n=1
            while (root/name).exists():name=f'{base}_{n}';n+=1
            folder=root/name;folder.mkdir()
            result=write_step(jobs,sources,axis,v['stock'],v['kerf'],v['add'],folder)
            (folder/'manifest.json').write_text(json.dumps({'axis':axis,'parameters':v,'jobs':jobs,'files':result,'sourceFiles':{str(k):str(p) for k,p in sources.items()}},ensure_ascii=False,indent=2),encoding='utf8')
            error=None
        except Exception as exc:
            error=f'{type(exc).__name__}: {exc}';result=None
            if folder and folder.exists():
                for p in folder.iterdir():p.unlink()
                folder.rmdir()
        try:self.after(0,lambda:self.export_done(snapshot,folder,result,error))
        except RuntimeError:pass
    def export_done(self,snapshot,folder,result,error):
        self.busy=False
        if self.jobs and snapshot[0]==self.generation:self.step.configure(state='normal')
        if error:self.status.set(error);messagebox.showerror('Ошибка STEP',error,parent=self)
        else:
            if snapshot[0]==self.generation:
                self.folder=folder;self.open.configure(state='normal')
            self.status.set(f'Готово: {folder}, STEP: {len(result)}')
    def open_folder(self):
        if self.folder and self.folder.exists():os.startfile(str(self.folder))
    def save_csv(self):
        if not self.jobs:return
        filename=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if not filename:return
        with open(filename,'w',encoding='utf-8-sig',newline='') as output:
            writer=csv.writer(output,delimiter=';');writer.writerow(['Хлыст','Детали','Отход, мм'])
            for job in self.jobs:writer.writerow([job['number'],' | '.join(p['name']+' '+str(p['length']) for p in job['parts']),job['waste']])
    def save_pdf(self):
        if not self.jobs:return
        filename=filedialog.asksaveasfilename(parent=self,defaultextension='.pdf',filetypes=[('PDF','*.pdf')],initialfile='NEST_report.pdf')
        if not filename:return
        try:
            from pdf_report import write_report
            write_report(filename,self.jobs,self.snapshot[1],self.snapshot[2])
        except ImportError:
            messagebox.showerror('Отчёт PDF','Не найден reportlab. Установите: C:/Python/python.exe -m pip install --user reportlab',parent=self)
        except Exception as exc:
            messagebox.showerror('Ошибка PDF',str(exc),parent=self)
        else:self.status.set('Сохранён отчёт PDF: '+filename)

if __name__=='__main__':Nest().mainloop()

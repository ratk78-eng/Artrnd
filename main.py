import csv,datetime,json,math,os,queue,threading,tkinter as tk
from pathlib import Path
from tkinter import ttk,filedialog,messagebox,simpledialog
from nesting import plan
from cad import dimensions,write_step
ROOT=Path(__file__).resolve().parent
AUTO='Авто'

def pick_axis(dims,choice):
    # 'Авто' takes the longest extent: for a tube that is its length
    return max('xyz',key=lambda a:dims[a]) if choice==AUTO else choice.lower()

def part_label(p):
    return p['name']+(f" [{p['axis'].upper()}]" if p.get('axis') else '')+' '+str(p['length'])

def mm(x):
    return f'{x:.2f}'.rstrip('0').rstrip('.')

def stock_state(waste,v):
    if waste>=v['overrun']:return 'Перерасход','over'
    if waste>v['maxwaste']:return 'Выше макс. отхода','warn'
    return 'OK','ok'

def ranges(numbers):
    # [1,2,3,5] -> '1–3, 5'
    out=[];start=prev=numbers[0]
    for n in numbers[1:]+[None]:
        if n is not None and n==prev+1:prev=n;continue
        out.append(str(start) if start==prev else f'{start}–{prev}');start=prev=n
    return ', '.join(out)

def group_jobs(jobs):
    """Stocks with the same parts in the same order and the same waste are shown as one row."""
    groups={}
    for j in jobs:
        key=(tuple((p['name'],p['length'],p.get('axis')) for p in j['parts']),round(j['waste'],6))
        groups.setdefault(key,[]).append(j)
    return list(groups.values())

def count_parts(parts):
    # identical parts inside one stock -> (part, count), in cutting order
    counted=[]
    for p in parts:
        if counted and (counted[-1][0]['name'],counted[-1][0]['length'],counted[-1][0].get('axis'))==(p['name'],p['length'],p.get('axis')):counted[-1][1]+=1
        else:counted.append([p,1])
    return counted

class Nest(tk.Tk):
    def __init__(self):
        super().__init__();self.title('NEST — нестинг труб');self.geometry('1100x860');self.minsize(900,700)
        self.rows=[];self.jobs=None;self.folder=None;self.generation=0;self.busy=False
        self.vars={key:tk.StringVar(value=default) for key,default in [('stock','6000'),('kerf','3'),('add','0'),('maxwaste','150'),('overrun','150')]}
        self.axis=tk.StringVar(value=AUTO);self.status=tk.StringVar(value='Готово к загрузке STEP/IGES.')
        self.pending=queue.Queue();self.inspections=queue.Queue()
        threading.Thread(target=self.inspect_worker,daemon=True).start()
        self.build();self.poll_inspections()
    def build(self):
        outer=ttk.Frame(self,padding=10);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='NEST — раскладка труб',font=('Segoe UI',17,'bold')).pack(anchor='w')
        settings=ttk.LabelFrame(outer,text='Параметры',padding=8);settings.pack(fill='x',pady=8)
        for i,(key,title) in enumerate([('stock','Хлыст, мм'),('kerf','Рез, мм'),('add','Припуск, мм'),('maxwaste','Макс. отход, мм'),('overrun','Перерасход, мм')]):
            ttk.Label(settings,text=title).grid(row=0,column=i,sticky='w',padx=5)
            e=ttk.Entry(settings,width=16,textvariable=self.vars[key]);e.grid(row=1,column=i,padx=5)
            e.bind('<KeyRelease>',lambda event:self.changed())
        ttk.Label(settings,text='Ось').grid(row=0,column=5)
        box=ttk.Combobox(settings,textvariable=self.axis,values=[AUTO,'X','Y','Z'],state='readonly',width=6);box.grid(row=1,column=5)
        box.bind('<<ComboboxSelected>>',self.axis_change)
        group=ttk.LabelFrame(outer,text='Детали — двойной щелчок: по «Ось» меняет ось X→Y→Z, по другим столбцам — количество',padding=7);group.pack(fill='x')
        columns=('file','x','y','z','axis','length','qty','state')
        self.table=ttk.Treeview(group,columns=columns,show='headings',selectmode='extended',height=6)
        for col,title,width in [('file','CAD-файл',225),('x','X, мм',80),('y','Y, мм',80),('z','Z, мм',80),('axis','Ось',50),('length','Длина, мм',95),('qty','Кол-во',65),('state','Статус',200)]:
            self.table.heading(col,text=title);self.table.column(col,width=width,anchor='w' if col in ('file','state') else 'center')
        self.table.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(group,command=self.table.yview);scroll.pack(side='right',fill='y');self.table.configure(yscrollcommand=scroll.set)
        self.table.bind('<Double-1>',self.double_click)
        row=ttk.Frame(outer);row.pack(fill='x',pady=5)
        for label,command in [('Добавить STEP/IGES',self.add_files),('Удалить строки',self.remove),('Пример',self.example),('Очистить',self.clear)]:ttk.Button(row,text=label,command=command).pack(side='left',padx=3)
        row=ttk.Frame(outer);row.pack(fill='x',pady=5)
        ttk.Button(row,text='Рассчитать',command=self.calculate).pack(side='left',padx=3)
        self.step=ttk.Button(row,text='Создать STEP',command=self.export,state='disabled');self.step.pack(side='left',padx=3)
        self.open=ttk.Button(row,text='Открыть папку',command=self.open_folder,state='disabled');self.open.pack(side='left',padx=3)
        self.csv=ttk.Button(row,text='CSV',command=self.save_csv,state='disabled');self.csv.pack(side='left',padx=3)
        self.pdf=ttk.Button(row,text='Отчёт PDF',command=self.save_pdf,state='disabled');self.pdf.pack(side='left',padx=3)
        result=ttk.LabelFrame(outer,text='Результат',padding=7);result.pack(fill='both',expand=True)
        self.summary=tk.StringVar(value='Нажмите «Рассчитать».')
        ttk.Label(result,textvariable=self.summary,font=('Segoe UI',10,'bold')).pack(anchor='w')
        frame=ttk.Frame(result);frame.pack(fill='both',expand=True,pady=(5,0))
        self.result=ttk.Treeview(frame,columns=('axis','length','qty','waste','state'),show='tree headings',selectmode='browse',height=8)
        self.result.heading('#0',text='Хлыст / деталь');self.result.column('#0',width=390,anchor='w')
        for col,title,width in [('axis','Ось',55),('length','Длина, мм',95),('qty','Кол-во',75),('waste','Отход, мм',95),('state','Статус',150)]:
            self.result.heading(col,text=title);self.result.column(col,width=width,anchor='w' if col=='state' else 'center')
        self.result.tag_configure('stock',font=('Segoe UI',9,'bold'))
        for tag,colour in [('ok','#1b7a3a'),('warn','#b36b00'),('over','#c62828')]:self.result.tag_configure(tag,foreground=colour)
        self.result.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(frame,command=self.result.yview);scroll.pack(side='right',fill='y');self.result.configure(yscrollcommand=scroll.set)
        self.result.bind('<<TreeviewSelect>>',lambda event:self.draw_bar())
        ttk.Label(result,text='Схема реза выбранного хлыста:').pack(anchor='w',pady=(6,0))
        self.bar=tk.Canvas(result,height=54,bg='white',highlightthickness=1,highlightbackground='#c8ced6');self.bar.pack(fill='x')
        self.bar.bind('<Configure>',lambda event:self.draw_bar())
        self.groups=[]
        ttk.Label(outer,textvariable=self.status,wraplength=1040).pack(fill='x',pady=4)
    def changed(self):
        self.generation+=1;self.jobs=None;self.folder=None
        self.step.configure(state='disabled');self.open.configure(state='disabled');self.csv.configure(state='disabled');self.pdf.configure(state='disabled')
        self.show_result(None,None)
    def show_result(self,jobs,v):
        self.result.delete(*self.result.get_children());self.groups=[];self.shown=v
        if not jobs:self.summary.set('Нажмите «Рассчитать».');self.draw_bar();return
        parts=[p for j in jobs for p in j['parts']];states=[stock_state(j['waste'],v)[1] for j in jobs]
        usage=100*sum(p['length'] for p in parts)/(len(jobs)*v['stock'])
        text=f"Хлыстов: {len(jobs)}     Деталей: {len(parts)}     Отход: {mm(sum(j['waste'] for j in jobs))} мм     Использование: {usage:.1f} %"
        if states.count('warn'):text+=f"     Выше макс. отхода: {states.count('warn')}"
        if states.count('over'):text+=f"     Перерасход: {states.count('over')}"
        self.summary.set(text)
        self.groups=group_jobs(jobs)
        for g,same in enumerate(self.groups):
            job=same[0];state,tag=stock_state(job['waste'],v);numbers=[j['number'] for j in same]
            title=(f'Хлыст {numbers[0]}' if len(same)==1 else f'Хлысты {ranges(numbers)}  ×{len(same)}')
            self.result.insert('','end',iid=f'g{g}',text=title,open=True,tags=('stock',tag),
                values=('','',f"{len(job['parts'])} шт.",mm(job['waste']),state))
            for k,(p,n) in enumerate(count_parts(job['parts'])):
                self.result.insert(f'g{g}','end',iid=f'g{g}p{k}',text=Path(p['name']).stem,
                    values=((p.get('axis') or '—').upper(),mm(p['length']),n,'',''))
        self.result.selection_set('g0')
    def draw_bar(self):
        c=self.bar;c.delete('all')
        selected=self.result.selection()
        if not self.groups or not selected:return
        job=self.groups[int(selected[0].split('p')[0][1:])][0];v=self.shown
        width=max(c.winfo_width(),200);left=8;scale=(width-2*left)/v['stock'];top,bottom=8,40;x=left
        for i,p in enumerate(job['parts']):
            w=(p['length']+v['add'])*scale
            c.create_rectangle(x,top,x+w,bottom,fill=('#9cc3e6','#c4dcf0')[i%2],outline='#4a78a8')
            label=Path(p['name']).stem+' · '+mm(p['length'])
            if w>60:c.create_text(x+w/2,(top+bottom)/2,text=label if len(label)*6<w else mm(p['length']),font=('Segoe UI',8))
            x+=w
            if i<len(job['parts'])-1:
                cut=max(v['kerf']*scale,2)  # a 3 mm cut is below one pixel at this scale
                c.create_rectangle(x,top,x+cut,bottom,fill='#333333',outline='');x+=cut
        if job['waste']>0 and x<width-left:
            state,tag=stock_state(job['waste'],v)
            c.create_rectangle(x,top,width-left,bottom,fill='#e6e6e6',outline='#9a9a9a',stipple='gray50')
            if width-left-x>50:c.create_text((x+width-left)/2,(top+bottom)/2,text='отход '+mm(job['waste']),font=('Segoe UI',8),fill={'ok':'#1b7a3a','warn':'#b36b00','over':'#c62828'}[tag])
        c.create_text(left,bottom+8,text='0',anchor='w',font=('Segoe UI',7),fill='#666666')
        c.create_text(width-left,bottom+8,text=mm(v['stock'])+' мм',anchor='e',font=('Segoe UI',7),fill='#666666')
    def refresh(self):
        self.table.delete(*self.table.get_children())
        for n,r in enumerate(self.rows):
            dims=r.get('dims') or {}
            self.table.insert('', 'end',iid=str(n),values=(r['name'],*(dims.get(a,'') for a in 'xyz'),(r.get('axis') or '').upper(),r.get('length',''),r['qty'],r['state']))
    def set_axis(self,row,axis):
        row['axis']=axis;row['length']=row['dims'][axis]
    def axis_change(self,event=None):
        selected=[self.rows[int(i)] for i in self.table.selection()]
        targets=[r for r in (selected or self.rows) if r.get('dims')]
        for r in targets:self.set_axis(r,pick_axis(r['dims'],self.axis.get()))
        self.changed();self.refresh()
        self.status.set(f'Ось изменена у строк: {len(targets)}'+(' (выделенные)' if selected else '')+'. Рассчитайте раскладку заново.')
    def add_files(self):
        names=filedialog.askopenfilenames(parent=self,filetypes=[('STEP / IGES','*.stp *.step *.igs *.iges')])
        if not names:return
        if self.rows and all(r['path'] is None for r in self.rows):self.rows.clear()
        self.changed()
        for filename in names:
            p=Path(filename)
            if p.suffix.lower() not in ('.stp','.step','.igs','.iges'):continue
            row={'id':max([r['id'] for r in self.rows],default=0)+1,'path':p,'name':p.name,'dims':None,'axis':None,'length':None,'qty':1,'state':'Чтение CAD…'}
            self.rows.append(row);self.pending.put(row)
        self.refresh();self.status.set('Определяются размеры X/Y/Z…')
    def inspect_worker(self):
        # OCP readers share global settings, so CAD files are read one at a time
        while True:
            row=self.pending.get()
            if not any(x is row for x in self.rows):continue
            try:dims=dimensions(row['path']);error=None
            except Exception as e:dims=None;error=f'{type(e).__name__}: {e}'
            self.inspections.put((row,dims,error))
    def poll_inspections(self):
        # Tk is touched only from the GUI thread: results are drained here
        while True:
            try:row,dims,error=self.inspections.get_nowait()
            except queue.Empty:break
            self.inspected(row,dims,error)
        self.poll_id=self.after(100,self.poll_inspections)
    def destroy(self):
        self.after_cancel(self.poll_id);super().destroy()
    def inspected(self,row,dims,error):
        if not any(x is row for x in self.rows):return
        if error:row['state']='Ошибка';self.status.set(row['name']+': '+error)
        else:row['dims']=dims;self.set_axis(row,pick_axis(dims,self.axis.get()));row['state']='Готово';self.status.set('Размеры загружены. Проверьте ось и количество.')
        self.refresh()
    def double_click(self,event):
        item=self.table.identify_row(event.y)
        if not item:return
        row=self.rows[int(item)]
        if self.table.column(self.table.identify_column(event.x),'id')=='axis':self.cycle_axis(row)
        else:self.edit_quantity(row)
    def cycle_axis(self,row):
        if not row.get('dims'):return
        self.set_axis(row,'xyz'[('xyz'.index(row['axis'])+1)%3]);self.changed();self.refresh()
        self.status.set(f"{row['name']}: ось {row['axis'].upper()}, длина {row['length']} мм. Рассчитайте раскладку заново.")
    def edit_quantity(self,row):
        value=simpledialog.askinteger('Количество','Количество деталей',parent=self,initialvalue=row['qty'],minvalue=1,maxvalue=10000)
        if value is not None:row['qty']=value;self.changed();self.refresh()
    def remove(self):
        for i in sorted((int(i) for i in self.table.selection()),reverse=True):self.rows.pop(i)
        self.changed();self.refresh()
    def clear(self):self.rows.clear();self.changed();self.refresh();self.status.set('Очищено')
    def example(self):
        self.rows=[{'id':i,'path':None,'name':'Пример '+str(length),'dims':None,'axis':None,'length':length,'qty':qty,'state':'Только расчёт'} for i,(length,qty) in enumerate([(1768,4),(1263,24),(1563,6)],1)]
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
                if r['path'] is not None and r.get('dims') is None:raise ValueError(('Ошибка чтения CAD: ' if r['state']=='Ошибка' else 'CAD ещё не прочитан: ')+r['name'])
            self.jobs=plan(self.rows,v['stock'],v['kerf'],v['add'])
            self.snapshot=(self.generation,v,{r['id']:r['path'] for r in self.rows if r['path']})
            self.show_result(self.jobs,v)
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
            _,v,sources=snapshot;root=ROOT/'exports';root.mkdir(parents=True,exist_ok=True)
            base=datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S');name=base;n=1
            while (root/name).exists():name=f'{base}_{n}';n+=1
            folder=root/name;folder.mkdir()
            result=write_step(jobs,sources,v['stock'],v['kerf'],v['add'],folder)
            (folder/'manifest.json').write_text(json.dumps({'layoutAxis':'x','parameters':v,'jobs':jobs,'files':result,'sourceFiles':{str(k):str(p) for k,p in sources.items()}},ensure_ascii=False,indent=2),encoding='utf8')
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
            for job in self.jobs:writer.writerow([job['number'],' | '.join(part_label(p) for p in job['parts']),job['waste']])
    def save_pdf(self):
        if not self.jobs:return
        filename=filedialog.asksaveasfilename(parent=self,defaultextension='.pdf',filetypes=[('PDF','*.pdf')],initialfile='NEST_report.pdf')
        if not filename:return
        try:
            from pdf_report import write_report
            write_report(filename,self.jobs,self.snapshot[1])
        except ImportError:
            messagebox.showerror('Отчёт PDF','Не найден reportlab. Установите: C:/Python/python.exe -m pip install --user reportlab',parent=self)
        except Exception as exc:
            messagebox.showerror('Ошибка PDF',str(exc),parent=self)
        else:self.status.set('Сохранён отчёт PDF: '+filename)

if __name__=='__main__':Nest().mainloop()

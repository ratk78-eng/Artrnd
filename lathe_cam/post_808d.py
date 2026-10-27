from datetime import datetime

# Postprocessor for SINUMERIK 808D ADVANCED T. Turns the neutral moves from cam.py into
# a main program (.MPF) and, in the default mode, one subprogram (.SPF) per contour.
# Syntax follows the 808D/802D sl turning manuals: G18 G90 G95 DIAMON, G96 S.. LIMS=..,
# T1 D1, CYCLE95 / CYCLE97. Before a part is cut, check the cycle parameter order against the
# operating manual of this exact control version (see README, "Что проверить на станке").

_RU='абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
_LAT=['a','b','v','g','d','e','e','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']
_TR={**{r:l for r,l in zip(_RU,_LAT)},**{r.upper():l.upper() for r,l in zip(_RU,_LAT)},'Ø':'D','×':'X'}

def ascii_text(text):
    """Comments go to the control transliterated: not every 808D font/USB import reads Cyrillic."""
    return ''.join(_TR.get(c,c if ord(c)<128 else '?') for c in text)

def fmt(v):
    s=f'{v:.3f}'.rstrip('0').rstrip('.')
    return '0' if s in ('-0','') else s

def _cycle_args(values):
    return ','.join('' if v is None else (f'"{v}"' if isinstance(v,str) else fmt(v) if isinstance(v,float) else str(v)) for v in values)

class Post:
    def __init__(self,machine,contour_mode='spf',vrt=False,now=None,ascii=True):
        if contour_mode not in ('spf','labels'):raise ValueError('contour_mode: spf или labels')
        self.m=machine;self.mode=contour_mode;self.vrt=vrt;self.ascii=ascii;self.now=now or datetime.now()

    def run(self,job):
        self.lines=[];self.n=0;self.g=None;self.x=self.z=self.f=None
        self.contours={}
        m=self.m
        self._raw(f'; {job.program}.MPF  {self.now:%Y-%m-%d %H:%M}')
        self._raw('; СОЗДАНО lathe_cam, ПРОВЕРЬТЕ НА СИМУЛЯЦИИ СТОЙКИ ПЕРЕД ОБРАБОТКОЙ')
        self._b('G18 G90 G95 G71 G40 DIAMON');self._b(m.work_offset)
        for mv in job.moves():getattr(self,'_'+mv[0])(*mv[1:])
        files={f'{job.program}.MPF':''}
        if self.mode=='labels':
            for name,c in self.contours.items():
                self._raw(f'; КОНТУР {name}');self._raw(f'{name}_S:');self._contour_blocks(c,self._b);self._raw(f'{name}_E:')
        files[f'{job.program}.MPF']='\n'.join(self.lines)+'\n'
        if self.mode=='spf':
            for name,c in self.contours.items():
                body=[f'; {name}.SPF  '+('KONTUR DLYA CYCLE95' if self.ascii else 'КОНТУР ДЛЯ CYCLE95'),'G18 G90 DIAMON']
                self._contour_blocks(c,body.append)
                body.append('M17')
                files[f'{name}.SPF']='\n'.join(body)+'\n'
        return files

    # --- output helpers
    def _raw(self,text):self.lines.append(ascii_text(text) if self.ascii else text)
    def _b(self,text):
        if self.m.line_numbers:
            self.n+=self.m.line_step;text=f'N{self.n} {text}'
        self.lines.append(text)
    def _move(self,g,x,z,f=None):
        words=[]
        if x is not None and x!=self.x:words.append('X'+fmt(x))
        if z is not None and z!=self.z:words.append('Z'+fmt(z))
        if not words:return   # already there
        if f is not None and f!=self.f:words.append('F'+fmt(f));self.f=f
        if g!=self.g:words.insert(0,g);self.g=g
        self.x=x if x is not None else self.x;self.z=z if z is not None else self.z
        self._b(' '.join(words))
    def _forget(self):self.g=self.x=self.z=self.f=None   # after a cycle the position is unknown

    def _contour_blocks(self,c,emit):
        emit(f'G1 X{fmt(c.start[0])} Z{fmt(c.start[1])}')
        for e in c.elements:
            if e['type']=='line':emit(f'G1 X{fmt(e["x"])} Z{fmt(e["z"])}')
            else:emit(f'{"G2" if e["dir"]=="cw" else "G3"} X{fmt(e["x"])} Z{fmt(e["z"])} CR={fmt(e["r"])}')

    # --- moves
    def _comment(self,text):self._raw('; '+text)
    def _tool(self,n,d,name):
        self._b('M5 '+self.m.coolant_off);self._forget()
        self._move('G0',self.m.tool_change_x,self.m.tool_change_z)
        self._b(f'T{n} D{d}')
    def _spindle(self,mode,value,lims):
        if mode=='css':self._b(f'G96 S{fmt(value)} LIMS={lims} {self.m.spindle_cw}')
        else:self._b(f'G97 S{value} {self.m.spindle_cw}')
    def _coolant(self,on):self._b(self.m.coolant_on if on else self.m.coolant_off)
    def _rapid(self,x,z):self._move('G0',x,z)
    def _feed(self,x,z,f):self._move('G1',x,z,f)
    def _dwell(self,sec):self._b(f'G4 F{fmt(sec)}');self.f=None   # G4 F is seconds and resets modal F
    def _cycle95(self,p):
        c=p['contour'];self.contours[c.name]=c
        npp=c.name if self.mode=='spf' else f'{c.name}_S:{c.name}_E'
        args=[npp,p['mid'],p['falz'],p['falx'],p['fal'],p['ff1'],p['ff2'],p['ff3'],p['vari'],None,None]
        if self.vrt:args.append(p['vrt'])
        self._b(f'CYCLE95({_cycle_args(args)})');self._forget()
    def _cycle97(self,p):
        args=[p['pit'],None,p['spl'],p['fpl'],p['dm1'],p['dm2'],p['app'],p['rop'],p['tdep'],p['fal'],
              p['iang'],p['nsp'],p['nrc'],p['nid'],p['vari'],p['numt']]
        if self.vrt:args.append(p['vrt'])
        self._b(f'CYCLE97({_cycle_args(args)})');self._forget()
    def _end(self):
        self._forget();self._move('G0',self.m.tool_change_x,self.m.tool_change_z)
        self._b('M5');self._b('M30')

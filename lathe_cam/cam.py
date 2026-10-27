from math import isfinite,ceil
from .contour import Contour,EPS

# The CAM side: a job (stock, contour, tools, operations) becomes a list of neutral moves.
# Nothing here knows the 808D syntax; post_808d.py turns the moves into a program.
CLEAR=2.   # safe distance from the stock, mm (X in diameter)

def _pos(op,key,default=None,allow_zero=False):
    v=op.get(key,default)
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not isfinite(v) or v<0 or (v==0 and not allow_zero):
        raise ValueError(f'Операция {op.get("type")}: неверный параметр {key}={v!r}')
    return float(v)

def _check_name(name,what):
    if not (name[:1].isascii() and name[:1].isalpha()) or not all(c.isascii() and (c.isalnum() or c=='_') for c in name) or len(name)>24:
        raise ValueError(what+': латинские буквы, цифры и _, начинается с буквы, до 24 символов')

class Job:
    def __init__(self,data,machine):
        self.machine=machine
        self.program=str(data.get('program','PART')).upper()
        _check_name(self.program,'Имя программы')
        st=data['stock']
        self.stock_d=_pos(st,'diameter');self.stock_len=_pos(st,'length');self.face=_pos(st,'face_allowance',0,True)
        if self.stock_d>machine.max_diameter:raise ValueError(f'Заготовка Ø{self.stock_d} больше максимума станка Ø{machine.max_diameter}')
        if self.stock_len>machine.max_length:raise ValueError(f'Заготовка {self.stock_len} мм длиннее РМЦ {machine.max_length}')
        self.contour=Contour.from_dict(data['contour']) if data.get('contour') else None
        if self.contour:
            self.contour.name=self.contour.name.upper();_check_name(self.contour.name,'Имя контура')
            if self.contour.name==self.program:raise ValueError('Имя контура совпадает с именем программы')
            b=self.contour.bounds()
            if b['x_max']>self.stock_d+2*CLEAR+EPS:raise ValueError(f'Контур Ø{b["x_max"]:g} больше заготовки Ø{self.stock_d:g}')
            if b['z_max']>EPS or b['z_min']<-self.stock_len-EPS:raise ValueError('Контур выходит за заготовку по Z (Z0 на торце детали)')
        self.tools={}
        for k,t in data.get('tools',{}).items():
            n=int(k);machine.check_tool(n)
            self.tools[n]={'name':str(t.get('name',f'T{n}')),'d':int(t.get('d',1)),'nose_radius':float(t.get('nose_radius',0.4))}
        self.operations=data.get('operations',[])
        if not self.operations:raise ValueError('Нет операций')

    def surface(self,z0,z1=None):
        """Diameter of the part after turning over this Z range (the stock where there is no contour)."""
        d=self.contour.diameter_at(z0,z1) if self.contour else None
        return min(d,self.stock_d) if d is not None else self.stock_d

    def tool(self,op):
        n=op.get('tool')
        if n not in self.tools:raise ValueError(f'Операция {op.get("type")}: инструмент {n} не описан в tools')
        return n,self.tools[n]

    def moves(self):
        out=[('comment',f'{self.machine.name} / {self.machine.control}'),
             ('comment',f'ЗАГОТОВКА D{self.stock_d:g} L{self.stock_len:g}, ПРИПУСК ТОРЦА {self.face:g}')]
        current=None
        for i,op in enumerate(self.operations,1):
            kind=op.get('type')
            fn=OPERATIONS.get(kind)
            if fn is None:raise ValueError(f'Операция {i}: неизвестный тип {kind!r}')
            n,t=self.tool(op)
            if kind in ('drill','thread') and 'rpm' not in op:raise ValueError(f'Операция {i} {kind}: нужны постоянные обороты rpm (G97)')
            out.append(('comment',f'--- {i}. {kind.upper()} T{n} {t["name"]} ---'))
            if n!=current:
                out.append(('tool',n,t['d'],t['name']));current=n
            out+=_spindle(op,self.machine)
            if op.get('coolant',True):out.append(('coolant',True))
            out+=fn(self,op,t)
        out+=[('coolant',False),('end',)]
        return out

def _spindle(op,m):
    if 'rpm' in op:
        rpm=int(_pos(op,'rpm'));m.check_rpm(rpm)
        return [('spindle','rpm',rpm,None)]
    vc=_pos(op,'vc');lims=int(op.get('lims',m.max_rpm));m.check_rpm(lims)
    return [('spindle','css',vc,lims)]

def _face(job,op,t):
    f=_pos(op,'feed');depth=_pos(op,'depth',1.)
    x0=job.stock_d+CLEAR;x1=-2*t['nose_radius']   # past the centre, so no pip is left
    passes=max(1,ceil(job.face/depth-EPS)) if job.face>EPS else 1
    out=[('rapid',x0,job.face+CLEAR)]
    for k in range(passes):
        z=job.face-(k+1)*job.face/passes if job.face>EPS else 0.
        out+=[('rapid',x0,z),('feed',x1,z,f),('rapid',x1,z+1),('rapid',x0,z+1)]
    return out

def _contour_cycle(job,op,t,vari):
    if not job.contour:raise ValueError(f'Операция {op["type"]}: в задании нет контура')
    rough=vari!=5
    out=[('rapid',job.stock_d+CLEAR,job.face+CLEAR)]
    out.append(('cycle95',{'contour':job.contour,
        'mid':_pos(op,'depth',2.) if rough else 0.,
        'falz':_pos(op,'allowance_z',0.1,True) if vari!=5 else 0.,
        'falx':_pos(op,'allowance_x',0.3,True) if vari!=5 else 0.,
        'fal':0.,
        'ff1':_pos(op,'feed',0.25) if rough else 0.,
        'ff2':_pos(op,'plunge_feed',0.1),
        'ff3':_pos(op,'finish_feed',op.get('feed',0.1)) if vari!=1 else _pos(op,'feed',0.25),
        'vari':vari,'vrt':_pos(op,'retract',1.)}))
    return out

def _rough(job,op,t):return _contour_cycle(job,op,t,1)
def _finish(job,op,t):return _contour_cycle(job,op,t,5)
def _complete(job,op,t):return _contour_cycle(job,op,t,9)

def _groove(job,op,t):
    # the grooving tool is set up on the corner facing the part face (Z+);
    # z is the groove wall nearer the face, the groove goes towards the chuck
    f=_pos(op,'feed');x=_pos(op,'x');z=float(op['z']);w=_pos(op,'width');iw=_pos(op,'insert_width')
    peck=_pos(op,'peck',0.,True);dwell=_pos(op,'dwell',0.,True)
    if iw>w+EPS:raise ValueError('Канавка уже пластины')
    x0=job.stock_d+CLEAR;last=z-w+iw
    n=max(1,ceil((z-last)/(0.8*iw)-EPS)) if z-last>EPS else 0
    zs=[z-(z-last)*k/n for k in range(n+1)] if n else [z]
    top=job.surface(z-0.05,z-w+0.05)   # walls touching the groove edge do not count
    if x>=top:raise ValueError(f'Дно канавки Ø{x:g} не ниже поверхности Ø{top:g}')
    out=[('rapid',x0,z)]
    for zz in zs:
        out.append(('rapid',x0,zz))
        out+=_plunge(top,x,zz,f,peck)
        if dwell:out.append(('dwell',dwell))
        out.append(('rapid',x0,zz))
    return out

def _plunge(top,x,z,f,peck):
    out=[('rapid',top+0.5,z)];cur=top+0.5
    if not peck:return out+[('feed',x,z,f)]
    while cur-2*peck>x+EPS:
        cur-=2*peck   # peck is radial, X is a diameter
        out+=[('feed',cur,z,f),('rapid',cur+1,z),('rapid',cur+0.2,z)]
    return out+[('feed',x,z,f)]

def _thread(job,op,t):
    p=_pos(op,'pitch');d=_pos(op,'x');zs=float(op.get('z_start',0.));ze=float(op['z_end'])
    if ze>=zs:raise ValueError('Резьба: z_end должен быть левее z_start')
    depth=_pos(op,'depth',round(0.6134*p,3))   # metric external thread depth on the radius
    return [('rapid',d+2*CLEAR,zs+_pos(op,'run_in',2*p)),
            ('cycle97',{'pit':p,'spl':zs,'fpl':ze,'dm1':d,'dm2':float(op.get('x_end',d)),
                'app':_pos(op,'run_in',2*p),'rop':_pos(op,'run_out',p),'tdep':depth,
                'fal':_pos(op,'finish_allowance',0.05,True),'iang':_pos(op,'infeed_angle',30.,True),
                'nsp':_pos(op,'start_angle',0.,True),'nrc':int(_pos(op,'passes',max(4,ceil(depth/0.12)))),
                'nid':int(_pos(op,'spring_passes',1,True)),'vari':int(op.get('vari',1)),
                'numt':int(_pos(op,'starts',1)),'vrt':_pos(op,'retract',1.)})]

def _drill(job,op,t):
    f=_pos(op,'feed');depth=_pos(op,'depth');peck=_pos(op,'peck',0.,True)
    out=[('rapid',0.,job.face+CLEAR)];cur=job.face
    if peck:
        while cur-peck>-depth+EPS:
            cur-=peck
            out+=[('feed',0.,cur,f),('rapid',0.,job.face+CLEAR),('rapid',0.,cur+0.5)]
    return out+[('feed',0.,-depth,f),('rapid',0.,job.face+CLEAR)]

def _part(job,op,t):
    f=_pos(op,'feed');z=float(op['z']);x_end=float(op.get('x_end',0.))
    if z>0:raise ValueError('Отрезка: z должен быть ≤ 0')
    slow=_pos(op,'slow_from',min(10.,job.stock_d))   # diameter where the feed is halved
    top=job.surface(z)
    out=[('rapid',job.stock_d+CLEAR,z),('rapid',top+0.5,z)]
    if x_end<slow<top:out.append(('feed',slow,z,f))
    return out+[('feed',x_end,z,f/2),('rapid',job.stock_d+CLEAR,z)]

OPERATIONS={'face':_face,'rough':_rough,'finish':_finish,'complete':_complete,
            'groove':_groove,'thread':_thread,'drill':_drill,'part':_part}

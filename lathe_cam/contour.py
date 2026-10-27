from math import atan2,cos,sin,hypot,pi,isfinite,ceil

# A part contour in the XZ plane, as the 808D programs it: X is a diameter, Z is along the
# spindle with Z0 on the finished face. Arcs use CR= (the shorter arc); "cw" is G2 and "ccw" is G3
# seen with Z to the right and X up, which is how the control draws the part.
EPS=1e-6

def _num(v,what):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not isfinite(v):raise ValueError('Неверное число: '+what)
    return float(v)

class Contour:
    def __init__(self,start,elements,name='KONTUR'):
        self.name=name
        self.start=(_num(start['x'],'начало X'),_num(start['z'],'начало Z'))
        self.elements=[]
        x,z=self.start
        for i,e in enumerate(elements,1):
            kind=e.get('type','line')
            nx=_num(e['x'],f'элемент {i} X');nz=_num(e['z'],f'элемент {i} Z')
            if nx<-EPS:raise ValueError(f'Элемент {i}: отрицательный диаметр')
            if abs(nx-x)<EPS and abs(nz-z)<EPS:raise ValueError(f'Элемент {i}: нулевая длина')
            if kind=='line':self.elements.append({'type':'line','x':nx,'z':nz})
            elif kind=='arc':
                r=_num(e['r'],f'элемент {i} R');d=e.get('dir','cw')
                if d not in ('cw','ccw'):raise ValueError(f'Элемент {i}: dir должно быть cw или ccw')
                if r<=0 or hypot((nx-x)/2,nz-z)>2*r+EPS:raise ValueError(f'Элемент {i}: радиус {r} меньше половины хорды')
                self.elements.append({'type':'arc','x':nx,'z':nz,'r':r,'dir':d})
            else:raise ValueError(f'Элемент {i}: неизвестный тип {kind}')
            x,z=nx,nz
        if not self.elements:raise ValueError('Пустой контур')

    @classmethod
    def from_dict(cls,d):return cls(d['start'],d['elements'],d.get('name','KONTUR'))

    def points(self,step=0.5):
        """Polyline (diameter, z) along the contour; arcs are split into chords of about `step` mm."""
        pts=[self.start];x,z=self.start
        for e in self.elements:
            if e['type']=='line':pts.append((e['x'],e['z']))
            else:pts+=_arc_points(x,z,e['x'],e['z'],e['r'],e['dir'],step)[1:]
            x,z=e['x'],e['z']
        return pts

    def diameter_at(self,z0,z1=None):
        """Largest contour diameter over Z from z0 to z1 (one point when z1 is None), or None if
        the contour does not reach there."""
        lo,hi=sorted((z0,z0 if z1 is None else z1));best=None
        pts=self.points()
        for (xa,za),(xb,zb) in zip(pts,pts[1:]):
            a,b=sorted((za,zb))
            if b<lo-EPS or a>hi+EPS:continue
            cands=[x for x,z in ((xa,za),(xb,zb)) if lo-EPS<=z<=hi+EPS]
            if abs(zb-za)>EPS:cands+=[xa+(xb-xa)*(min(max(zz,a),b)-za)/(zb-za) for zz in (lo,hi)]
            best=max([best]+cands) if best is not None else max(cands)
        return best

    def bounds(self):
        pts=self.points()
        return {'x_min':min(p[0] for p in pts),'x_max':max(p[0] for p in pts),'z_min':min(p[1] for p in pts),'z_max':max(p[1] for p in pts)}

def _arc_points(x0,z0,x1,z1,r,d,step):
    # work in (z, radius) so the arc is a true circle
    a=(z0,x0/2);b=(z1,x1/2)
    mz,mr=(a[0]+b[0])/2,(a[1]+b[1])/2
    dz,dr=b[0]-a[0],b[1]-a[1];chord=hypot(dz,dr)
    h=max(r*r-chord*chord/4,0)**.5
    # centre to the right of the travel for G2 (clockwise), to the left for G3
    s=-1 if d=='cw' else 1
    cz,cr=mz-s*h*dr/chord,mr+s*h*dz/chord
    t0=atan2(a[1]-cr,a[0]-cz);t1=atan2(b[1]-cr,b[0]-cz)
    sweep=t1-t0
    if d=='cw' and sweep>0:sweep-=2*pi
    if d=='ccw' and sweep<0:sweep+=2*pi
    n=max(2,ceil(abs(sweep)*r/step))
    return [(2*(cr+r*sin(t0+sweep*k/n)),cz+r*cos(t0+sweep*k/n)) for k in range(n+1)]

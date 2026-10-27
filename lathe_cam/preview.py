from html import escape

# SVG sketch of a job: stock, finished contour, explicit tool moves (feed solid, rapid dashed).
# Cycles (CYCLE95/97) are drawn as their contour/thread only; the control expands them itself.
def svg(job,moves=None,scale=4.,margin=20):
    moves=moves if moves is not None else job.moves()
    zs=[job.face+5,-job.stock_len-5];rs=[0,job.stock_d/2+5]
    for mv in moves:
        if mv[0] in ('rapid','feed'):rs.append(abs(mv[1])/2);zs.append(mv[2])
    zmin,zmax=min(zs),max(zs);rmax=max(rs)
    w=(zmax-zmin)*scale+2*margin;h=rmax*scale+2*margin
    def q(x,z):return (z-zmin)*scale+margin,h-margin-x/2*scale
    def p(x,z):return '%.1f,%.1f'%q(x,z)
    def line(a,b,style):return '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '%(q(*a)+q(*b))+style+'/>'
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" font-family="sans-serif" font-size="12">',
         f'<rect width="100%" height="100%" fill="white"/>',
         f'<text x="{margin}" y="14">{escape(job.program)}: заготовка Ø{job.stock_d:g}×{job.stock_len:g}</text>',
         f'<line x1="0" y1="{h-margin:.1f}" x2="{w:.0f}" y2="{h-margin:.1f}" stroke="#888" stroke-dasharray="8 3 2 3"/>',
         f'<polygon points="{p(0,job.face)} {p(job.stock_d,job.face)} {p(job.stock_d,-job.stock_len)} {p(0,-job.stock_len)}" fill="#eee" stroke="#999"/>']
    if job.contour:
        out.append(f'<polyline points="{" ".join(p(*pt) for pt in job.contour.points())}" fill="none" stroke="#000" stroke-width="2"/>')
    last=None
    for mv in moves:
        if mv[0] in ('rapid','feed'):
            if last:
                style='stroke="#c00" stroke-dasharray="4 3"' if mv[0]=='rapid' else 'stroke="#06c" stroke-width="1.5"'
                out.append(line(last,(mv[1],mv[2]),style))
            last=(mv[1],mv[2])
        elif mv[0]=='cycle97':
            c=mv[1];out.append(line((c['dm1'],c['spl']),(c['dm2'],c['fpl']),'stroke="#090" stroke-width="3" stroke-dasharray="2 2"'))
        elif mv[0] in ('tool','cycle95'):last=None
    out.append('</svg>')
    return '\n'.join(out)+'\n'

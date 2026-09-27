from math import ceil,isfinite

# A cut is needed only between parts: n parts take sum(length+allowance)+(n-1)*kerf <= stock,
# which is the same as sum(length+allowance+kerf) <= stock+kerf. The search works with these
# "sizes" and this "capacity", so the kerf is handled exactly.
EPS=1e-6
SEARCH_BUDGET=300000  # search nodes for the whole plan; keeps big orders under a few seconds

def lower_bound(rows,stock,kerf,allowance):
    """No layout can use fewer stocks than this."""
    total=sum((r['length']+allowance+kerf)*r['qty'] for r in rows)
    return max(1,ceil(total/(stock+kerf)-EPS))

def _best_fit(parts,stock,kerf,allowance):
    """Best fit decreasing: each part goes to the stock where it leaves the smallest rest."""
    bins=[]
    for part in parts:
        target=None;least=float('inf')
        for b in bins:
            rest=stock-b['used']-part['length']-allowance-kerf
            if rest>=-1e-8 and rest<least:target=b;least=rest
        if target is None:bins.append({'parts':[part],'used':part['length']+allowance})
        else:target['parts'].append(part);target['used']+=part['length']+allowance+kerf
    return [b['parts'] for b in bins]

def _fullest(sizes,counts,capacity,limit,force_largest):
    """Counts per size that fill one stock as full as possible (depth-first, node limited)."""
    n=len(sizes);take=[0]*n;best=[-1.,None];nodes=[0]
    rest=[0.]*(n+1)
    for i in range(n-1,-1,-1):rest[i]=rest[i+1]+sizes[i]*counts[i]
    def search(i,fill):
        nodes[0]+=1
        if fill>best[0]+EPS:best[0]=fill;best[1]=take[:]
        if i==n or best[0]>=capacity-EPS or nodes[0]>limit or fill+rest[i]<=best[0]+EPS:return
        most=min(counts[i],int((capacity-fill+EPS)//sizes[i]))
        least=1 if force_largest and i==0 else 0
        for k in range(most,least-1,-1):  # many pieces first: a good layout is found early
            take[i]=k;search(i+1,fill+k*sizes[i])
            if best[0]>=capacity-EPS or nodes[0]>limit:break
        take[i]=0
    search(0,0.)
    return best[1]

def _fullest_first(parts,stock,kerf,allowance,limit,force_largest):
    """Fill every next stock with the fullest possible set of the remaining parts."""
    pools={}
    for p in parts:pools.setdefault(p['length'],[]).append(p)
    capacity=stock+kerf;bins=[]
    while pools:
        lengths=sorted(pools,reverse=True)
        take=_fullest([l+allowance+kerf for l in lengths],[len(pools[l]) for l in lengths],capacity,limit,force_largest)
        chosen=[]
        for l,k in zip(lengths,take):
            chosen+=pools[l][:k];del pools[l][:k]
            if not pools[l]:del pools[l]
        if not chosen:return None
        bins.append(chosen)
    return bins

def _score(bins,stock,kerf,allowance):
    # fewer stocks first; with the same count prefer waste gathered in few stocks (one long offcut
    # instead of many short ones), which also keeps more stocks under the waste thresholds
    wastes=[stock-sum(p['length']+allowance for p in b)-(len(b)-1)*kerf for b in bins]
    return (len(bins),-sum(w*w for w in wastes))

def plan(rows,stock,kerf,allowance):
    if not all(isfinite(x) for x in (stock,kerf,allowance)) or stock<=0 or min(kerf,allowance)<0:
        raise ValueError('Неверные параметры раскладки')
    parts=[]
    for row in rows:
        length=row['length'];qty=row['qty']
        if not isinstance(qty,int) or qty<1 or not isfinite(length) or length<=0 or length+allowance>stock:
            raise ValueError('Неверная длина/количество: '+row['name'])
        if len(parts)+qty>10000:raise ValueError('Максимум 10000 деталей')
        parts.extend({'source':row['id'],'length':length,'name':row['name'],'axis':row.get('axis')} for _ in range(qty))
    if not parts:raise ValueError('Нет деталей')
    parts.sort(key=lambda p:p['length'],reverse=True)
    candidates=[_best_fit(parts,stock,kerf,allowance)]
    bound=lower_bound(rows,stock,kerf,allowance)
    if len(candidates[0])>bound and len({p['length'] for p in parts})<500:  # search depth = distinct lengths
        limit=max(500,SEARCH_BUDGET//bound)
        for force in (True,False):
            bins=_fullest_first(parts,stock,kerf,allowance,limit,force)
            if bins:candidates.append(bins)
    bins=min(candidates,key=lambda b:_score(b,stock,kerf,allowance))
    bins=[sorted(b,key=lambda p:p['length'],reverse=True) for b in bins]
    jobs=[{'number':i+1,'parts':b,'waste':stock-sum(p['length']+allowance for p in b)-(len(b)-1)*kerf} for i,b in enumerate(bins)]
    if sorted(id(p) for j in jobs for p in j['parts'])!=sorted(id(p) for p in parts):raise ArithmeticError('Детали потеряны при раскладке')
    for j in jobs:
        if j['waste']< -EPS:raise ArithmeticError('Ошибка баланса хлыста')
    return jobs

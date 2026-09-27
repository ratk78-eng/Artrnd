from math import isfinite

def plan(rows,stock,kerf,allowance):
    if not all(isfinite(x) for x in (stock,kerf,allowance)) or stock<=0 or min(kerf,allowance)<0:
        raise ValueError('Неверные параметры раскладки')
    parts=[]
    for row in rows:
        length=row['length'];qty=row['qty']
        if not isinstance(qty,int) or qty<1 or not isfinite(length) or length<=0 or length+allowance>stock:
            raise ValueError('Неверная длина/количество: '+row['name'])
        if len(parts)+qty>10000:raise ValueError('Максимум 10000 деталей')
        parts.extend({'source':row['id'],'length':length,'name':row['name']} for _ in range(qty))
    if not parts:raise ValueError('Нет деталей')
    parts.sort(key=lambda p:p['length'],reverse=True)
    bins=[]
    for part in parts:
        target=None;least=float('inf')
        for b in bins:
            rest=stock-b['used']-part['length']-allowance-kerf
            if rest>=-1e-8 and rest<least:target=b;least=rest
        if target is None:bins.append({'parts':[part],'used':part['length']+allowance})
        else:target['parts'].append(part);target['used']+=part['length']+allowance+kerf
    jobs=[{'number':i+1,'parts':b['parts'],'waste':stock-b['used']} for i,b in enumerate(bins)]
    for j in jobs:
        actual=sum(p['length']+allowance for p in j['parts'])+(len(j['parts'])-1)*kerf
        if abs(stock-j['waste']-actual)>1e-6 or j['waste']< -1e-6:raise ArithmeticError('Ошибка баланса хлыста')
    return jobs

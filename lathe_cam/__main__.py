import argparse,json,os,sys
from .machine import load
from .cam import Job
from .post_808d import Post
from .preview import svg

def build(job_path,out_dir,machine_path=None,contour_mode='spf',vrt=False,line_numbers=None):
    with open(job_path,encoding='utf-8') as f:data=json.load(f)
    overrides={'line_numbers':line_numbers} if line_numbers is not None else None
    job=Job(data,load(machine_path,overrides))
    moves=job.moves()
    files=Post(job.machine,contour_mode,vrt).run(job)
    os.makedirs(out_dir,exist_ok=True)
    written=[]
    for name,text in files.items():
        path=os.path.join(out_dir,name)
        with open(path,'w',encoding='ascii',newline='\r\n') as f:f.write(text)
        written.append(path)
    path=os.path.join(out_dir,job.program+'.svg')
    with open(path,'w',encoding='utf-8') as f:f.write(svg(job,moves))
    return written+[path]

def main(argv=None):
    a=argparse.ArgumentParser(prog='python -m lathe_cam',description='Токарный CAM для KMT-KE / SINUMERIK 808D')
    a.add_argument('job',help='задание JSON')
    a.add_argument('-o','--out',default='exports',help='папка для программ (по умолчанию exports)')
    a.add_argument('-m','--machine',help='параметры станка JSON (по умолчанию KMT-KE)')
    a.add_argument('--labels',action='store_true',help='контур в той же программе по меткам, без .SPF')
    a.add_argument('--vrt',action='store_true',help='добавлять параметр _VRT в CYCLE95/CYCLE97')
    a.add_argument('-n','--line-numbers',action='store_true',default=None,help='нумеровать кадры N10, N20...')
    args=a.parse_args(argv)
    try:files=build(args.job,args.out,args.machine,'labels' if args.labels else 'spf',args.vrt,args.line_numbers)
    except (ValueError,KeyError,OSError) as e:
        print('Ошибка:',e,file=sys.stderr);return 1
    for f in files:print(f)
    return 0

if __name__=='__main__':sys.exit(main())

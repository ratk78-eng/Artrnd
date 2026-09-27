"""NEST CAD backend. OCP imported on demand."""
from pathlib import Path

def load(path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IGESControl import IGESControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    path=Path(path)
    reader=STEPControl_Reader() if path.suffix.lower() in ('.stp','.step') else IGESControl_Reader()
    if reader.ReadFile(str(path))!=IFSelect_RetDone:raise ValueError('Не читается CAD: '+path.name)
    reader.TransferRoots();shape=reader.OneShape()
    if shape.IsNull():raise ValueError('Пустая CAD-модель: '+path.name)
    it=TopExp_Explorer(shape,TopAbs_SOLID);n=0
    while it.More():n+=1;it.Next()
    if n!=1:raise ValueError(f'{path.name}: требуется одно твёрдое тело, найдено {n}')
    return shape

def extent(shape,axis):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box=Bnd_Box();BRepBndLib.Add_s(shape,box)
    if box.IsVoid():raise ValueError('Пустой CAD-габарит')
    lo=box.CornerMin();hi=box.CornerMax()
    index='xyz'.index(axis)
    lower=(lo.X(),lo.Y(),lo.Z())
    upper=(hi.X(),hi.Y(),hi.Z())
    return lower[index],upper[index]

def dimensions(path):
    shape=load(path)
    return {axis:round(extent(shape,axis)[1]-extent(shape,axis)[0],5) for axis in 'xyz'}

def prolong(shape,axis,amount,name):
    if amount==0:return shape
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE,TopAbs_SOLID
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.gp import gp_Vec
    as_face=getattr(TopoDS,'Face_s',None) or TopoDS.Face  # OCP 8 renamed Face_s to Face
    original_start,end=extent(shape,axis);faces=[];it=TopExp_Explorer(shape,TopAbs_FACE)
    while it.More():
        face=as_face(it.Current());lo,hi=extent(face,axis)
        if abs(lo-end)<0.02 and abs(hi-end)<0.02 and BRepAdaptor_Surface(face).GetType()==GeomAbs_Plane:faces.append(face)
        it.Next()
    if len(faces)!=1:raise ValueError(name+': для припуска нужен ровно один плоский конечный торец')
    delta=[0.,0.,0.];delta['xyz'.index(axis)]=amount
    prism=BRepPrimAPI_MakePrism(faces[0],gp_Vec(*delta)).Shape()
    fused=BRepAlgoAPI_Fuse(shape,prism);fused.Build()
    if not fused.IsDone():raise ValueError('Не удалось удлинить '+name)
    result=fused.Shape();it=TopExp_Explorer(result,TopAbs_SOLID);count=0
    while it.More():count+=1;it.Next()
    start,finish=extent(result,axis)
    if count!=1 or not BRepCheck_Analyzer(result).IsValid() or abs(start-original_start)>0.1 or abs(finish-end-amount)>0.1:
        raise ValueError('Некорректный припуск: '+name)
    return result

def write_step(jobs,source_paths,axis,stock,kerf,allowance,folder):
    from OCP.gp import gp_Vec,gp_Trsf
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs,STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    originals={key:load(path) for key,path in source_paths.items()}
    enlarged={key:prolong(shape,axis,allowance,Path(source_paths[key]).name) for key,shape in originals.items()}
    written=[]
    for job in jobs:
        compound=TopoDS_Compound();builder=BRep_Builder();builder.MakeCompound(compound)
        position=0.
        for i,p in enumerate(job['parts']):
            key=p['source'];start,end=extent(originals[key],axis);length=end-start
            if abs(length-p['length'])>0.1:raise ValueError('Длина CAD отличается от расчёта: '+Path(source_paths[key]).name)
            offset=[0.,0.,0.];offset['xyz'.index(axis)]=position-start
            tr=gp_Trsf();tr.SetTranslation(gp_Vec(*offset))
            builder.Add(compound,BRepBuilderAPI_Transform(enlarged[key],tr,True).Shape())
            position+=length+allowance+(kerf if i<len(job['parts'])-1 else 0)
        if position>stock+0.02 or abs(stock-position-job['waste'])>0.11:raise ValueError('Баланс хлыста не сошёлся')
        doc=TDocStd_Document(TCollection_ExtendedString('NEST'))
        tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main());label=tool.AddShape(compound,True)
        if label.IsNull():raise RuntimeError('Не создана STEP-сборка')
        writer=STEPCAFControl_Writer();writer.SetNameMode(True)
        if not writer.Transfer(doc,STEPControl_AsIs):raise RuntimeError('Не удалось перенести сборку в STEP')
        path=folder/f"stock_{job['number']:03d}.step"
        if writer.Write(str(path))!=IFSelect_RetDone or not path.is_file():raise RuntimeError('Ошибка записи STEP')
        reader=STEPControl_Reader()
        if reader.ReadFile(str(path))!=IFSelect_RetDone:raise RuntimeError('STEP не открывается после записи')
        reader.TransferRoots();result=reader.OneShape()
        if result.IsNull():raise RuntimeError('STEP пуст после записи')
        it=TopExp_Explorer(result,TopAbs_SOLID);count=0
        while it.More():count+=1;it.Next()
        lo,hi=extent(result,axis)
        if count!=len(job['parts']) or abs(lo)>0.2 or abs(hi-position)>0.2:
            raise RuntimeError(f'Проверка экспорта не пройдена: {path.name}, тел {count}, ожидается {len(job["parts"])}')
        written.append(path.name)
    return written

from datetime import datetime
from html import escape
from pathlib import Path

def write_report(filename,jobs,values):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Spacer
    fonts=[Path('C:/Windows/Fonts/arial.ttf'),Path('C:/Windows/Fonts/segoeui.ttf')]
    font=next((p for p in fonts if p.is_file()),None)
    if font is None:raise RuntimeError('Не найден шрифт Arial или Segoe UI')
    pdfmetrics.registerFont(TTFont('NestUnicode',str(font)))
    normal=ParagraphStyle('normal',fontName='NestUnicode',fontSize=9,leading=13,spaceAfter=5)
    heading=ParagraphStyle('heading',parent=normal,fontSize=15,leading=19,spaceAfter=12)
    sub=ParagraphStyle('sub',parent=normal,fontSize=11,leading=15,spaceBefore=8)
    doc=SimpleDocTemplate(str(filename),pagesize=A4,rightMargin=33,leftMargin=33,topMargin=35,bottomMargin=35)
    P=lambda s,style=normal:Paragraph(escape(str(s)),style)
    total_parts=sum(len(j['parts']) for j in jobs);total_waste=sum(j['waste'] for j in jobs)
    elements=[P('NEST — отчёт раскладки труб',heading),P('Создан: '+datetime.now().strftime('%d.%m.%Y %H:%M:%S')),
        P('Хлыст: '+str(values['stock'])+' мм; рез: '+str(values['kerf'])+' мм; припуск: '+str(values['add'])+' мм.'),
        P('Хлыстов: '+str(len(jobs))+'; деталей: '+str(total_parts)+'; общий отход: '+f'{total_waste:.2f}'+' мм.'),
        P('Макс. отход: '+str(values['maxwaste'])+' мм; порог перерасхода: '+str(values['overrun'])+' мм.'),
        P('Ось длины задаётся для каждой детали; в STEP-сборке детали выкладываются вдоль оси X.'),
        P('Рез учитывается только между деталями; пороги отхода обозначают статус, но не гарантируются.')]
    for job in jobs:
        waste=job['waste'];state=('Перерасход' if waste>=values['overrun'] else 'Выше макс. отхода' if waste>values['maxwaste'] else 'ОК')
        data=[[P('№'),P('Деталь / файл'),P('Ось'),P('Длина, мм'),P('Припуск, мм')]]
        for n,part in enumerate(job['parts'],1):data.append([P(n),P(part['name']),P((part.get('axis') or '—').upper()),P(f"{part['length']:.2f}"),P(f"{values['add']:.2f}")])
        table=Table(data,colWidths=[32,237,40,100,100],repeatRows=1,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e8f0fa')),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#d2d9e3')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]))
        elements.extend([P('Хлыст '+str(job['number'])+' — отход '+f'{waste:.2f}'+' мм; '+state,sub),table,Spacer(1,9)])
    doc.build(elements)
    if not Path(filename).is_file() or Path(filename).stat().st_size==0:raise RuntimeError('PDF не создан')
    return filename

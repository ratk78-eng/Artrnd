"""Run on 64-bit Windows from the tested NEST_DESKTOP source folder."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
STAGE=ROOT/'release'/'stage'
DIST=ROOT/'release'/'dist'
WORK=ROOT/'release'/'work'
SPEC=ROOT/'release'/'spec'
ISCC_CANDIDATES=[Path(os.environ.get('INNO_ISCC','')) if os.environ.get('INNO_ISCC') else None,
    Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Inno Setup 6'/'ISCC.exe',
    Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Inno Setup 6'/'ISCC.exe']

def run(*args,attempts=1):
    # On some machines Windows randomly refuses CreateProcess (WinError 5) or python.exe crashes;
    # steps that spawn many Python processes are retried instead of failing the whole build
    for attempt in range(1,attempts+1):
        print('>',*[str(a) for a in args],flush=True)
        try:
            subprocess.run([str(a) for a in args],cwd=ROOT,check=True);return
        except (subprocess.CalledProcessError,OSError) as exc:
            if attempt==attempts:raise
            print(f'Сбой запуска ({exc}); повтор {attempt+1} из {attempts}…',flush=True)

def main():
    if sys.platform!='win32' or sys.maxsize<=2**32:raise RuntimeError('Собирать только на 64-битной Windows')
    for name in ('main.py','cad.py','nesting.py','pdf_report.py','NEST.iss'):
        if not (ROOT/name).is_file():raise FileNotFoundError('Не найден '+name+'; поместите сборочный комплект рядом с проверенными исходниками')
    for name in ('main.py','cad.py','nesting.py','pdf_report.py'):
        ast.parse((ROOT/name).read_text(encoding='utf-8'))
    run(sys.executable,'-c','import tkinter; import OCP.STEPControl; import OCP.STEPCAFControl; import reportlab; print("Dependencies OK")',attempts=3)
    if (ROOT/'test_nesting.py').is_file():run(sys.executable,'-m','unittest','test_nesting.py')
    STAGE.mkdir(parents=True,exist_ok=True)
    for name in ('cad.py','nesting.py','pdf_report.py'):shutil.copy2(ROOT/name,STAGE/name)
    original=(ROOT/'main.py').read_text(encoding='utf-8')
    old='ROOT=Path(__file__).resolve().parent'
    new="ROOT=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'NEST'"
    if original.count(old)!=1:raise RuntimeError('Не удалось безопасно перенести папку exports. Ожидается одна строка ROOT=Path(__file__).resolve().parent')
    changed=original.replace(old,new,1)
    ast.parse(changed)
    (STAGE/'main.py').write_text(changed,encoding='utf-8')
    print('Экспорт будет в %LOCALAPPDATA%\\NEST\\exports',flush=True)
    run(sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
        '--name','NEST','--distpath',DIST,'--workpath',WORK,'--specpath',SPEC,
        '--paths',STAGE,'--collect-all','OCP','--collect-all','reportlab',
        '--hidden-import','cad','--hidden-import','nesting','--hidden-import','pdf_report',STAGE/'main.py',attempts=3)
    executable=DIST/'NEST'/'NEST.exe'
    if not executable.is_file():raise RuntimeError('PyInstaller не создал NEST.exe')
    print('Приложение собрано:',executable,flush=True)
    iscc=next((p for p in ISCC_CANDIDATES if p and p.is_file()),None)
    if not iscc:raise RuntimeError('NEST.exe собран, но Inno Setup 6 не найден. Установите Inno Setup 6 и повторите сборку; либо задайте INNO_ISCC=путь к ISCC.exe')
    run(iscc,ROOT/'NEST.iss')
    installer=ROOT/'release'/'installer'/'NEST_Setup.exe'
    if not installer.is_file() or installer.stat().st_size==0:raise RuntimeError('Установщик не создан')
    print('ГОТОВО:',installer,flush=True)
    print('Обязательно проверьте установку на компьютере без Python и OCP.',flush=True)
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('ОШИБКА:',type(exc).__name__,str(exc),file=sys.stderr,flush=True)
        sys.exit(1)

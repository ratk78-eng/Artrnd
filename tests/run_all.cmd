@echo off
rem All tests. The building ones need KOMPAS running and the app on 127.0.0.1:8765; every built
rem part is also checked for features KOMPAS marks with an error (kcheck.py).
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
for %%t in (parse_reading_test exclude_test arc_outline_test clean_shapes_test arrange_test fillet_plan_test constraints_test) do (
  echo ===== %%t
  C:\Python\python.exe %%t.py 2>&1
)
echo ===== dxf_test
C:\Python\python.exe dxf_test.py 2>&1
for %%t in (test2_cavity_test drawing_test) do (
  echo ===== %%t --build
  C:\Python\python.exe %%t.py --build 2>&1
)
echo ===== steel --build
C:\Python\python.exe steel_test.py steel_dxf --build 2>&1 | findstr /C:"PASSED" /C:"FAILED" /C:"Error" /C:"KOMPAS ERROR"
echo ===== revolve --build
C:\Python\python.exe revolve_test.py revolve\gost.pdf --build 2>&1 | findstr /C:"PASSED" /C:"FAILED" /C:"Error" /C:"KOMPAS ERROR" /C:"built"
echo ===== plate --build
C:\Python\python.exe pdf_vector_test.py --build 2>&1
echo ===== shaft --build
C:\Python\python.exe pdf_shaft_test.py --build 2>&1
echo ===== gear --build
C:\Python\python.exe pdf_gear_test.py --build 2>&1
echo ===== knife --build
C:\Python\python.exe pdf_knife_test.py --build 2>&1
echo ===== wheel --build
C:\Python\python.exe pdf_wheel_test.py --build 2>&1
echo ===== turned_solid --build
C:\Python\python.exe turned_solid_test.py --build 2>&1
echo ===== raster_tube --build
C:\Python\python.exe raster_tube_test.py --build 2>&1
echo ===== clamp --build
C:\Python\python.exe pdf_clamp_test.py --build 2>&1
echo ===== done

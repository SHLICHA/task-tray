@echo off
rem Локальная сборка. Нужен Python 3.9+ (python.org, галочка "Add to PATH").
rem Для установщика дополнительно нужен Inno Setup 6: https://jrsoftware.org/isdl.php
cd /d "%~dp0"
python -m pip install --upgrade PySide6 pyinstaller || goto :err
python -m PyInstaller --noconfirm --noconsole --name Tasks --icon installer\tasks.ico task_tray.py || goto :err
set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if exist "%ISCC%" (
  "%ISCC%" installer\tasks.iss || goto :err
  echo Готово: %~dp0dist\TasksSetup.exe
) else (
  echo Inno Setup не найден - собрана только папка dist\Tasks
)
explorer dist
pause
exit /b 0
:err
echo Ошибка сборки.
pause
exit /b 1

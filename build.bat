@echo off
rem Сборка Tasks.exe. Нужен установленный Python 3.9+ (python.org, галочка "Add to PATH").
cd /d "%~dp0"
python -m pip install --upgrade PySide6 pyinstaller || goto :err
python -m PyInstaller --noconfirm --noconsole --onefile --name Tasks task_tray.py || goto :err
echo.
echo Готово: %~dp0dist\Tasks.exe
explorer dist
pause
exit /b 0
:err
echo Ошибка сборки.
pause
exit /b 1

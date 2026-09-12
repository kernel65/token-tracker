@echo off
rem === Token Tracker: панель управления (без окна консоли) ===
cd /d "%~dp0"
setlocal
rem preferred install path (if present), then PATH lookups
if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" launcher.pyw
  exit /b
)
where pythonw >nul 2>nul && (start "" pythonw launcher.pyw & exit /b)
where pyw >nul 2>nul && (start "" pyw launcher.pyw & exit /b)
where py >nul 2>nul && (start "" py -w launcher.pyw & exit /b)
where python >nul 2>nul && (start "" /min python launcher.pyw & exit /b)
echo Не найден Python. Установи с python.org и повтори.
pause

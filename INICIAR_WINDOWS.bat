@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
 set "PY=py"
) else (
 where python >nul 2>nul
 if %errorlevel% neq 0 goto missing
 set "PY=python"
)
%PY% --version || goto missing
%PY% -m pip install -r requirements.txt || goto failed
%PY% app.py
pause
exit /b
:missing
echo.
echo ERROR: Python no esta instalado o no esta en PATH.
echo Instala Python desde https://www.python.org/downloads/
echo Cierra y abre VS Code despues de instalarlo.
pause
exit /b 1
:failed
echo ERROR: No se pudieron instalar las dependencias.
pause
exit /b 1

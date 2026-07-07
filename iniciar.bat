@echo off
cd /d "%~dp0"
echo Iniciando Portal Dorel Peru...
echo.
if exist "venv\Scripts\streamlit.exe" (
    venv\Scripts\streamlit.exe run app.py
) else if exist "..\..\..\venv\Scripts\streamlit.exe" (
    ..\..\..\venv\Scripts\streamlit.exe run app.py
) else (
    echo [ERROR] No se encontro venv\Scripts\streamlit.exe
    echo         Ejecuta primero: actualizar.bat
)
pause

@echo off
echo ========================================
echo   ACTUALIZANDO PORTAL DOREL PERU
echo ========================================
echo.

cd /d "%~dp0"

:: Evitar que git abra el editor (vim) en cualquier escenario
set GIT_MERGE_AUTOEDIT=no
set GIT_EDITOR=true

:: Limpiar swap files de vim que pudieran haber quedado de corridas anteriores
if exist ".git\.MERGE_MSG.swp" del /f /q ".git\.MERGE_MSG.swp" >nul 2>&1
if exist ".git\.COMMIT_EDITMSG.swp" del /f /q ".git\.COMMIT_EDITMSG.swp" >nul 2>&1
if exist ".git\.TAG_EDITMSG.swp" del /f /q ".git\.TAG_EDITMSG.swp" >nul 2>&1

:: Abortar merge/rebase a medio terminar que hayan quedado pegados
if exist ".git\MERGE_HEAD" git merge --abort >nul 2>&1
if exist ".git\rebase-merge" git rebase --abort >nul 2>&1
if exist ".git\rebase-apply" git rebase --abort >nul 2>&1

:: Limpiar archivos en CONFLICTO que hayan quedado pegados de una corrida anterior.
:: (Caso tipico: un 'git stash pop' choco con app.py y nunca se resolvio, dejando
::  el repo en estado "needs merge". Esto NO crea MERGE_HEAD, por eso el bloque de
::  arriba no lo detecta y el pull falla en cada actualizacion.)
set "_STUCK="
for /f "delims=" %%f in ('git ls-files -u 2^>nul') do set "_STUCK=1"
if defined _STUCK (
    echo Limpiando conflictos pendientes de una actualizacion anterior...
    git reset -q --hard HEAD >nul 2>&1
)

:: Proteger archivos locales que no deben sincronizarse
git update-index --skip-worktree users.json >nul 2>&1

:: Sacar del tracking archivos de datos locales (resultados simulacion)
git rm --cached data/inputs/metadata.json >nul 2>&1
git rm --cached data/inputs/proy_result.parquet >nul 2>&1
for /f "delims=" %%f in ('git ls-files data/inputs/*.parquet 2^>nul') do (
    git rm --cached "%%f" >nul 2>&1
)

:: Resolver conflictos en esos archivos si existen
git checkout -- data/inputs/metadata.json >nul 2>&1
git checkout -- data/inputs/proy_result.parquet >nul 2>&1

:: Intentar pull directo (sin abrir editor)
git pull --no-edit origin main
if %ERRORLEVEL% EQU 0 (
    goto fin
)

:: Si fallo, usar stash como fallback
echo Guardando configuracion local...
git stash -q 2>nul
git pull --no-edit origin main
git stash pop -q 2>nul

:: Si el 'stash pop' dejo archivos en conflicto, NO dejar el repo trabado:
:: se mantiene la version del repositorio y el stash se conserva intacto
:: (git no borra el stash cuando el pop falla), asi nada se pierde.
set "_CONF="
for /f "delims=" %%f in ('git ls-files -u 2^>nul') do set "_CONF=1"
if defined _CONF (
    echo.
    echo [AVISO] Tus cambios locales chocaron con la nueva version.
    echo         Se mantiene la version oficial del repositorio.
    echo         Tus cambios siguen guardados — recuperalos con: git stash list
    echo.
    git reset -q --hard HEAD >nul 2>&1
)

:fin
echo.
echo ========================================
echo   INSTALANDO DEPENDENCIAS
echo ========================================
echo.
:: pip install -r requirements.txt — usa el venv local si existe.
:: Si requirements.txt no cambio, pip es rapido (todas dependencies satisfechas).
:: Si hay paquetes nuevos (ej: streamlit-pivot), los instala automaticamente.
if exist "venv\Scripts\python.exe" (
    echo Usando entorno virtual: venv\
    venv\Scripts\python.exe -m pip install -r requirements.txt --quiet --disable-pip-version-check
) else if exist ".venv\Scripts\python.exe" (
    echo Usando entorno virtual: .venv\
    .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet --disable-pip-version-check
) else (
    echo No se detecto venv\ o .venv\ — usando Python global
    python -m pip install -r requirements.txt --quiet --disable-pip-version-check
)
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [AVISO] Algunas dependencias pueden no haberse instalado.
    echo         Si la app falla con ImportError, corre manualmente:
    echo         pip install -r requirements.txt
    echo.
)

echo.
echo ========================================
echo   PORTAL ACTUALIZADO
echo ========================================
echo.
pause

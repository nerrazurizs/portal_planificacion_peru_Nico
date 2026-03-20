@echo off
echo ========================================
echo   ACTUALIZANDO PORTAL DOREL PERU
echo ========================================
echo.

cd /d "%~dp0"

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

:: Intentar pull directo
git pull origin main
if %ERRORLEVEL% EQU 0 (
    goto fin
)

:: Si fallo, usar stash como fallback
echo Guardando configuracion local...
git stash -q 2>nul
git pull origin main
git stash pop -q 2>nul

:fin
echo.
echo ========================================
echo   PORTAL ACTUALIZADO
echo ========================================
echo.
pause

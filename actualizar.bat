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

:: Sacar users.json del control de versiones SIN borrar el archivo de disco.
:: En origin/main users.json esta gitignored (no es parte del repo), pero en
:: algunas PCs quedo trackeado en el indice con skip-worktree de corridas viejas.
:: Eso hace fallar el 'git reset --hard' con "Entry 'users.json' not uptodate.
:: Cannot merge". Limpiamos el bit y lo des-trackeamos: el archivo (los usuarios
:: locales) se conserva intacto en disco y deja de estorbar al reset.
git update-index --no-skip-worktree users.json >nul 2>&1
git rm --cached users.json >nul 2>&1

:: Sacar del tracking archivos de datos locales (resultados simulacion)
git rm --cached data/inputs/metadata.json >nul 2>&1
git rm --cached data/inputs/proy_result.parquet >nul 2>&1
for /f "delims=" %%f in ('git ls-files data/inputs/*.parquet 2^>nul') do (
    git rm --cached "%%f" >nul 2>&1
)

:: Resolver conflictos en esos archivos si existen
git checkout -- data/inputs/metadata.json >nul 2>&1
git checkout -- data/inputs/proy_result.parquet >nul 2>&1

:: Sincronizar con el repositorio oficial.
:: El portal de despliegue debe ser un ESPEJO EXACTO de origin/main. Por eso NO
:: usamos 'git pull' (que intenta MEZCLAR y se atora en cada uno de estos casos):
::   - "Pulling is not possible because you have unmerged files" (conflicto pegado)
::   - "untracked working tree files would be overwritten by merge" (copias sueltas)
::   - merge/rebase a medio terminar
:: En su lugar: git fetch + git reset --hard origin/main, que FUERZA el estado del
:: repo y resuelve los tres casos de una. Es seguro para los archivos locales:
::   - users.json esta en .gitignore -> no se toca
::   - los resultados de simulacion (data/inputs/*.parquet, metadata.json) son
::     UNTRACKED y 'git reset --hard' nunca borra archivos untracked
echo Sincronizando con el repositorio...
git fetch --prune origin main
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] No se pudo conectar con GitHub. Revisa tu conexion a internet
    echo         e intenta de nuevo. El portal sigue con la version actual.
    echo.
    goto fin
)
git reset --hard origin/main

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

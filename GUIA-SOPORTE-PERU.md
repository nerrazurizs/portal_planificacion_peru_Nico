# 🛠️ Guía de soporte — Portal Dorel Perú

Guía para resolver los problemas más comunes al **actualizar** o **abrir** el portal.
Está pensada para seguirse paso a paso, o para pegársela a **Claude** y que te guíe.

> **Regla de oro:** casi todos los comandos van **dentro de la carpeta del portal**.
> Si tu ventana negra (`cmd`) dice `C:\Users\tu.usuario>` y NO `...\dorel-portal-peru>`,
> primero entra a la carpeta con:
> ```cmd
> cd dorel-portal-peru
> ```

---

## 📋 Índice de problemas

| Si te pasa esto... | Ve a la sección |
|---|---|
| El portal **no actualiza**: errores de git (`unmerged files`, `untracked files would be overwritten`, `needs merge`) | [1. El portal no actualiza](#1-el-portal-no-actualiza) |
| Error `Entry 'users.json' not uptodate. Cannot merge.` | [2. Error con users.json](#2-error-con-usersjson) |
| El portal **no abre**: `iniciar.bat` solo dice *"Presione una tecla para continuar"* y se cierra | [3. El portal no abre (venv/Python roto)](#3-el-portal-no-abre) |
| Quiero pedirle ayuda a Claude | [4. Cómo pedir ayuda a Claude](#4-cómo-pedir-ayuda-a-claude) |

---

## 1. El portal no actualiza

**Síntomas** — al correr `actualizar.bat` (o `git pull`) aparece alguno de estos:

```
error: Pulling is not possible because you have unmerged files.
fatal: Exiting because of an unresolved conflict.
```
```
error: The following untracked working tree files would be overwritten by merge:
        modules/analisis_forecast.py
        ...
Please move or remove them before you merge.
```

**Por qué pasa** — el repositorio local quedó "atascado" con conflictos o con copias
sueltas de archivos. `git pull` intenta **mezclar** y se traba.

### ✅ Solución universal: forzar el estado oficial del repo

Estos dos comandos dejan la carpeta **idéntica al repositorio oficial** (es el
"botón de resetear"). **No se pierden** tus usuarios ni tus datos de simulación
(`users.json` y `data/inputs/*.parquet` están protegidos).

```cmd
cd dorel-portal-peru
git fetch origin main
git reset --hard origin/main
```

Debe terminar con un mensaje tipo `HEAD is now at xxxxxxx ...`. Listo, ya está al día.

> Después de esto, el `actualizar.bat` ya quedó actualizado y la próxima vez se
> auto-limpia solo. Solo tienes que abrirlo normal.

---

## 2. Error con users.json

**Síntoma** — al hacer `git reset --hard origin/main` aparece:

```
error: Entry 'users.json' not uptodate. Cannot merge.
fatal: Could not reset index file to revision 'origin/main'.
```

**Por qué pasa** — en esa PC el archivo `users.json` quedó "trackeado" por git de
una configuración vieja, y choca con el reset.

### ✅ Solución (conserva los usuarios locales)

Corre estas **3 líneas en orden**. El archivo `users.json` **se queda en disco con
todos tus usuarios** — solo se saca del control de git.

```cmd
git update-index --no-skip-worktree users.json
git rm --cached users.json
git reset --hard origin/main
```

- Si la 2ª línea dice `did not match any files`, ignóralo y sigue con la 3ª.
- Debe terminar con `HEAD is now at xxxxxxx ...`.

> 💡 Respaldo opcional antes de empezar (por si acaso):
> ```cmd
> copy /Y users.json users.json.bak
> ```

---

## 3. El portal no abre

**Síntoma** — al hacer doble clic en `iniciar.bat`, la ventana negra solo muestra
*"Presione una tecla para continuar..."* y se cierra. El portal nunca aparece en el
navegador.

**Qué significa** — `iniciar.bat` intenta arrancar el portal y se cierra de
inmediato porque **falla al iniciar**. Las dos causas más comunes:

- **A)** Faltan dependencias en el entorno (`venv`).
- **B)** El `python.exe` del entorno está roto porque **Python ya no está instalado**
  en la PC (lo desinstalaron o lo movieron).

### 🔍 Paso 1 — Ver el error real

`iniciar.bat` se traga el mensaje. Para verlo, corre el portal a mano **llamando
directo al ejecutable del venv** (NO uses `python -m streamlit`: el `python` pelado
puede resolver al alias roto de Microsoft Store y morir en silencio):

```cmd
cd dorel-portal-peru
venv\Scripts\streamlit.exe run app.py
```

Según lo que pase:

| Lo que ves | Significa | Ve a |
|---|---|---|
| `Local URL: http://localhost:8501` y se queda ahí | ✅ Está funcionando — abre esa dirección en el navegador | — |
| `no se reconoce ... streamlit.exe` o `no existe` | Falta instalar dependencias en el venv | Paso 2A |
| `ModuleNotFoundError: No module named '...'` | Falta una dependencia | Paso 2A |
| **Nada** (vuelve al prompt al instante, en blanco) | El venv está roto (Python base desinstalado) | Paso 2B |
| Pide `Email:` y se queda esperando | Es el saludo de Streamlit | Aprieta **Enter** (vacío) y sigue |

### 🔍 Paso 1b — Confirmar si el venv está roto

Si en el paso anterior salió **en blanco**, comprueba con esto:

```cmd
venv\Scripts\python.exe --version
venv\Scripts\python.exe -c "print(123)"
```

- Si `--version` imprime `Python 3.11.x` **pero** `print(123)` sale **en blanco**
  → el venv está roto (Python base desinstalado). Ve al **Paso 2B**.

### ✅ Paso 2A — Reinstalar dependencias

Si solo faltaban paquetes:

```cmd
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Espera a que termine (unos minutos, va imprimiendo descargas) y vuelve a:

```cmd
venv\Scripts\python.exe -m streamlit run app.py
```

### ✅ Paso 2B — Reinstalar Python y recrear el venv

Si el venv está roto, hay que reinstalar Python:

**1) Instalar Python 3.11**
   - Descárgalo de **https://www.python.org/downloads/** (versión 3.11.x).
   - Corre el instalador y, **MUY IMPORTANTE**, antes de instalar marca la casilla:
     > ☑️ **Add python.exe to PATH**  *(abajo del instalador)*
   - Luego haz clic en **Install Now**.

**2) Cierra el instalador y cierra la ventana `cmd`. Abre una NUEVA ventana `cmd`**
   (para que reconozca el Python recién instalado).

**3) Recrear el entorno** (borrar `venv` es seguro, se regenera completo):
```cmd
cd dorel-portal-peru
rmdir /s /q venv
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

**4) Levantar el portal:**
```cmd
venv\Scripts\python.exe -m streamlit run app.py
```
o doble clic a `iniciar.bat`.

El portal abre en el navegador en **http://localhost:8501**.

---

## 4. Cómo pedir ayuda a Claude

Si algo no sale como dice esta guía, abre **Claude** y cuéntale el problema. Para
que te ayude rápido, dale **siempre**:

1. **Qué estabas haciendo** (ej: "corrí `actualizar.bat` y salió un error").
2. **Un screenshot de la ventana negra (`cmd`) completa** — que se vea el comando y
   el mensaje de error tal cual. Es lo más útil.
3. **En qué carpeta estás** (la primera parte del prompt, ej:
   `C:\Users\angela.berrospi\dorel-portal-peru>`).

> Ejemplo de mensaje para Claude:
> *"El portal de Perú no abre. Hago doble clic en `iniciar.bat` y solo dice
> 'Presione una tecla para continuar'. Te paso el screenshot de lo que sale cuando
> corro el comando a mano."*

---

## 📎 Referencia rápida (copiar/pegar)

```cmd
:: --- Entrar a la carpeta (siempre primero) ---
cd dorel-portal-peru

:: --- Destrabar / actualizar el repo (botón universal) ---
git fetch origin main
git reset --hard origin/main

:: --- Si se queja de users.json ---
git update-index --no-skip-worktree users.json
git rm --cached users.json
git reset --hard origin/main

:: --- Recrear el entorno (si el portal no abre y el venv está roto) ---
rmdir /s /q venv
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

:: --- Abrir el portal a mano (para ver errores) ---
venv\Scripts\streamlit.exe run app.py
```

El portal corre en: **http://localhost:8501**

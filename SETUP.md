# Setup Rapido - Portal Supply Chain Dorel

## Requisitos previos

- **Git** instalado ([descargar](https://git-scm.com/downloads))
- **Python 3.10+** instalado ([descargar](https://www.python.org/downloads/))
- **Cuenta GitHub** (pedir acceso al repo a Camilo)

Para verificar que tienes todo, abre una terminal (cmd o PowerShell en Windows) y ejecuta:

```bash
git --version
python --version
```

Si ambos responden con una version, estas listo.

---

## Paso 1: Clonar el repositorio

```bash
git clone https://github.com/TU-USUARIO/dorel-supply-chain.git
cd dorel-supply-chain
```

> Reemplaza la URL con la real del repositorio. Si no la tienes, pidela a Camilo.

---

## Paso 2: Crear entorno virtual (recomendado)

```bash
python -m venv venv
```

Activar el entorno:

- **Mac/Linux**: `source venv/bin/activate`
- **Windows (cmd)**: `venv\Scripts\activate`
- **Windows (PowerShell)**: `venv\Scripts\Activate.ps1`

> Cuando el entorno esta activo, veras `(venv)` al inicio de la linea.

---

## Paso 3: Instalar dependencias

```bash
pip install -r requirements.txt
```

Esto instala todo lo necesario (streamlit, pandas, snowflake, plotly, etc.).

---

## Paso 4: Configurar credenciales

Copiar el archivo de ejemplo y editarlo:

- **Mac/Linux**: `cp .env.example .env`
- **Windows**: `copy .env.example .env`

Abrir `.env` con cualquier editor de texto y completar:

```
APP_USER=queryplanificacion
APP_PASSWORD=PEDIR_A_CAMILO

SNOWFLAKE_USER=PEDIR_A_CAMILO
SNOWFLAKE_PASSWORD=PEDIR_A_CAMILO
SNOWFLAKE_ACCOUNT=DOREL-DJ_CHL
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=PUBLIC
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
```

> Las credenciales las entrega Camilo por chat o correo. NUNCA subirlas a Git.

---

## Paso 5: Ejecutar la app

```bash
streamlit run app.py
```

Se abrira el navegador en `http://localhost:8501`. Login con las credenciales del APP_USER.

---

## Resumen express (copy-paste)

Todo junto, para los apurados:

```bash
git clone URL_DEL_REPO
cd dorel-supply-chain
python -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
cp .env.example .env
# Editar .env con credenciales
streamlit run app.py
```

---

## Problemas frecuentes

| Problema | Solucion |
|----------|----------|
| `python: command not found` | Probar con `python3` en vez de `python` |
| `pip: command not found` | Probar con `pip3` o `python -m pip` |
| Error de Snowflake al conectar | Verificar usuario/password en `.env` |
| `ModuleNotFoundError` | Verificar que el entorno virtual esta activo `(venv)` |
| Puerto 8501 ocupado | `streamlit run app.py --server.port 8502` |
| La app no carga datos | Hacer clic en "Refrescar Datos" en el sidebar |

---

## Actualizaciones futuras

Cuando haya cambios en el codigo:

```bash
git pull
pip install -r requirements.txt    # solo si hay nuevas dependencias
streamlit run app.py
```

---

## Estructura del proyecto (referencia)

```
app.py              <- Punto de entrada principal
config.py           <- Colores, CSS, constantes
requirements.txt    <- Dependencias Python
.env                <- Credenciales (NO subir a Git)
.env.example        <- Plantilla de credenciales

db/
  connection.py     <- Conexion a Snowflake
  queries.py        <- Queries SQL
  cache.py          <- Cache centralizado de consultas

modules/            <- 20 modulos analiticos
  proyeccion.py     <- Motor de simulacion de stock
  plan_compras.py   <- Plan de compras
  ventas.py         <- Dashboard de ventas
  stock.py          <- Snapshot de stock
  comex.py          <- Tracking importaciones
  ...y 15 mas

utils/
  filters.py        <- Normalizacion de datos
  export.py         <- Exportar Excel/CSV/PPT
  auth.py           <- Autenticacion
```

# Portal de Planificación Dorel Perú — contexto del proyecto

Portal Streamlit de supply chain para Perú, migrado a **Streamlit in Snowflake (SiS)**.
No confundir con `Portal Planificacion (Camilo)\dorel-supply-portal` — ese es el portal de **Chile**, un proyecto hermano completamente distinto (otra cuenta/base de datos, otro stack de despliegue con Snow CLI).

## Arquitectura (SiS)

- `db/connection.py` usa `get_active_session()` de Snowpark — sin `.env`, sin `snowflake-connector-python`, sin credenciales.
- `utils/auth.py` — login vía SSO de Snowflake (`st.user.email`). No hay roles ni `users.json`: todos los usuarios autenticados ven todos los módulos (lista plana `ALL_MODULES`).
- `utils/file_persistence.py` — sin filesystem. Archivos subidos por el usuario y resultados de proyección viven en `st.session_state` durante la sesión del browser (se pierden al cerrar). `get_saved_file_path(key)` / `load_projection_results()` son la forma correcta de leer esos datos desde cualquier módulo — **nunca** leer de `data/inputs/*.parquet|*.xlsx` directamente, esos paths no existen en SiS.
- Sin PPTX (`python-pptx` no está en el registry de Snowflake), sin `kaleido`, sin `streamlit-pivot`. Export queda en CSV + Excel (`utils/export.py`).
- `db/cache.py::run_sql()` — el connector de SiS no procesa placeholders `%s` como el connector standalone. Cualquier query parametrizada debe pasar por `run_sql()`/`_run_sql` (quoting manual), nunca `pd.read_sql(sql, conn, params=...)` directo.
- `environment.yml` es el manifiesto real de dependencias (canal `snowflake`). `requirements.txt` quedó solo para referencia histórica de desarrollo local — ya no se puede correr `streamlit run app.py` localmente porque `get_active_session()` solo existe dentro de Snowflake.

## Modelo de despliegue — IMPORTANTE

**No hay integración git ni CI/CD.** La app corre en Snowflake como `DB_PLANIFICACION.PUBLIC.PORTAL_PLANIFICACION` (o el nombre que corresponda) y el código se actualiza **pegando/subiendo los archivos manualmente en el editor de Snowsight**. Este repo de GitHub es solo el lugar donde se versiona y prepara el código — la fuente de verdad de lo que está *corriendo* es lo que está pegado en Snowsight, que puede haber sido modificado ahí directamente sin que quede rastro en git.

Cuando se actualiza el portal, hay que decirle al usuario exactamente qué archivos cambiaron/se crearon para que los suba a mano.

## Remotes — dos repos distintos

- `origin` → `camilo-onate/dorel-portal-peru` — repo original de Camilo. Su rama `main` sigue activa: **Camilo desarrolla ahí, en la arquitectura vieja** (pre-SiS: `snowflake-connector`, `.env`, roles vía `users.json`, PPTX, etc.). No tiene los cambios de la migración a SiS.
- `nico` → `nerrazurizs/portal_planificacion_peru_Nico` — fork del usuario. La rama `sis-migration` (la que realmente importa) vive acá. **Pushear siempre a `nico`, nunca a `origin/main`** (no somos owners de ese repo).

## Cómo traer cambios nuevos de Camilo (`origin/main`) a SiS

Camilo sigue agregando features directo en `origin/main` sin adaptarlas a SiS. Cada vez que avise de cambios:

1. `git fetch origin` y revisar `git log <último-merge>..origin/main --oneline` para ver qué hay nuevo.
2. Crear una rama de prueba desde `sis-migration` y hacer `git merge origin/main --no-commit --no-ff` ahí (nunca directo sobre `sis-migration`).
3. Resolver conflictos favoreciendo el modelo SiS: sin roles/JSON, sin filesystem, sin PPTX, queries parametrizadas vía `run_sql`.
4. Auditar cada archivo nuevo/modificado por patrones incompatibles con SiS:
   - `snowflake.connector`, `dotenv`/`.env`
   - placeholders `%s` fuera de `run_sql`/`_run_sql`
   - lecturas de disco esperando archivos que el viejo `file_persistence` escribía (`data/inputs/*.parquet`, `*.xlsx`) — adaptar a `load_projection_results()` / `get_saved_file_path()`
   - `python-pptx`, `kaleido`, `streamlit_pivot`
   - llamadas de red salientes (`smtplib`, `requests`, etc. — SiS no tiene egress por defecto)
5. Verificar sintaxis (`python -c "import ast; ast.parse(open(f).read())"`) de todo lo tocado.
6. Commit, merge a `sis-migration`, push a `nico`.
7. Avisar al usuario la lista exacta de archivos a pegar/subir en Snowsight (incluyendo archivos binarios nuevos, ej. `data/inputs/leyendas.xlsx`).

## Pendientes conocidos (no resueltos, no son bugs de esta migración)

- **Alertas Email** (`modules/alertas_email.py`, `utils/email_sender.py`) — usa `smtplib` con SMTP saliente. SiS bloquea egress por defecto; necesitaría una External Access Integration a nivel de cuenta (requiere admin de Snowflake/IT).
- **`scripts/` (3 jobs standalone)** — `save_abc_xyz_snapshot.py`, `send_daily_alerts.py`, `send_stock_critico_tiendas.py` siguen con `snowflake.connector` + `dotenv`. No corren dentro de SiS; seguirían necesitando un servidor/cron externo o convertirse a Snowflake Tasks.
- **`"Diagnostico Tablas"`** está en el menú (`app.py`) y en `MODULE_DISPATCH`, pero falta en `ALL_MODULES` (`utils/auth.py`) — por eso nunca aparece en el sidebar aunque el código exista. Bug preexistente, no introducido por la migración.
- **`data/tipo_almacen.csv`** (usado opcionalmente por `modules/inbound.py`) no está trackeado en ningún repo — si se quiere esa enriquecida, hay que subirlo a mano a Snowsight; si no existe, el módulo simplemente omite esa columna sin error.

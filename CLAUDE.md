# Dorel Portal Perú — Contexto para Claude Code

## Identidad del Proyecto

Portal analítico de **supply chain para Dorel Perú**, construido con **Streamlit + Snowflake**.
Simula inventario, genera proyecciones de compra/abastecimiento y apoya la planificación.

**Idioma UI**: Español Perú (columnas, mensajes, labels). Moneda: **USD/PEN** (TC budget Perú).
**Idioma código**: Inglés (variables, funciones, docstrings).

---

## Flujo de Trabajo Git (OBLIGATORIO)

**Regla de oro: una rama = un cambio, corta y desechable.** Cada feature o fix
sale de `main` actualizado, vuelve a `main` por PR, y se borra. Vida útil:
**días, no semanas.** Está PROHIBIDO acumular trabajo en una rama de larga data
que junta features no relacionadas — es el anti-patrón que rompe el proyecto y
genera PRs imposibles de revisar.

### Ciclo estándar

```bash
git checkout main && git pull origin main      # 1. Partir SIEMPRE de main fresco
git checkout -b fix/descripcion-corta          # 2. Rama nueva por cada tarea
# ...trabajar, commitear...
git push -u origin fix/descripcion-corta        # 3. Pushear (solo si el usuario lo pide)
gh pr create --base main                        # 4. PR SIEMPRE contra main
# tras merge:
git checkout main && git pull && git branch -d fix/descripcion-corta   # 5. Borrar la rama
```

### Reglas duras

1. **PR siempre contra `main`**, nunca contra otra rama de features. Si dos
   cambios dependen entre sí, mergeá el primero a `main` y rebasá el segundo
   sobre `main` — NO los apiles en un stack de ramas.
2. **Si una rama supera ~1 semana o ~300 líneas, hay que partirla.** Es señal
   de que son varios cambios disfrazados de uno. Un PR = un tema = revisable en minutos.
3. **Sincronizar con `git pull --rebase origin main`** (no `merge`) mientras la
   rama es local y no compartida → historial lineal, sin commits "Merge branch 'main' into…".
4. **No trackear artefactos de datos** (`*.parquet`, `*.csv`/`*.xlsx` de datos,
   `users.json`, resultados de simulación). Los binarios/datos siempre chocan en
   merge y no se resuelven automáticamente. Van en `.gitignore` o se comparten por
   separado. ⚠️ Ojo: hoy `data/inputs/compra.csv` está trackeado y fue el que infló
   el PR #42 con 36.000 líneas — ese tipo de archivo NO debe ir en los PRs.
5. **Nunca commitear ni pushear sin que el usuario lo pida explícitamente.**
6. **Nombres de rama**: prefijo `feat/`, `fix/`, `data/` o `chore/` + descripción
   corta en kebab-case. Ej: `feat/cumplimiento-comex`, `fix/export-excel-seek`.

### Las máquinas de despliegue NO desarrollan

Las PCs de Perú que **solo usan** el portal nunca crean ramas ni commitean. Solo
siguen `main` con el "botón universal":
```bash
git fetch origin main && git reset --hard origin/main
```
Detalle y troubleshooting en [GUIA-SOPORTE-PERU.md](GUIA-SOPORTE-PERU.md).
Flujo completo de PRs para quien desarrolla en [GUIA-FLUJO-PR.md](GUIA-FLUJO-PR.md).

### Por qué (lección aprendida)

A junio 2026 el repo tenía **22 ramas abiertas y 0 mergeadas a `main`**, y el PR #42
(`backup/wip-antes-reset`) vivió fuera de `main`, arrastró un `compra.csv` de 36.000
líneas y terminó en conflicto (`CONFLICTING`). Resultado: el proyecto "se rompía"
constantemente y nada cuadraba. Ramas cortas que salen de `main` y vuelven a `main`
evitan todo eso: nunca se desfasan, los conflictos son raros y chicos, y los PRs se
revisan rápido.

---

## Mindset de Desarrollo

1. **Defensivo**: Nunca asumir que una columna existe. Siempre `if col in df.columns` o `.get(col, default)`.
2. **Fallback siempre**: Cada dato crítico (precio, costo, factor) tiene jerarquía de respaldo.
3. **Editar, no crear**: Preferir modificar archivos existentes. Módulos nuevos solo si hay necesidad estructural clara.
4. **Coerción numérica segura**: `pd.to_numeric(col, errors="coerce").fillna(0)` para evitar TypeErrors silenciosos.
5. **División segura**: `np.where(denominador > 0, numerador / denominador, default)` para evitar div/0.

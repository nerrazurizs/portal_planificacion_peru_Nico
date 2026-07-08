# 🔀 Flujo de trabajo con Git y PRs — Portal Dorel Perú

Guía para que el equipo trabaje sin romper el proyecto. Síguela paso a paso o
pégasela a Claude para que te guíe.

> **Modelo (igual que el portal de Chile):** todo sale **desde `main`** y vuelve
> **hacia `main`**, en ramas **cortas** que se borran al mergear. Nada de ramas
> locales eternas que nunca cuadran.

---

## 🩺 Por qué existe esta guía

Diagnóstico real del repo a hoy:

- **22 ramas** abiertas y **0 mergeadas** a `main` → trabajo disperso que nunca cierra.
- PRs **gigantes** con archivos de **datos commiteados** (ej: `data/inputs/compra.csv`,
  36.000 líneas) → conflictos y ruido.
- Ramas que divergen tanto de `main` que terminan en **conflicto** y no se pueden mergear.

Las reglas de abajo atacan exactamente eso.

---

## ✅ Las 7 reglas de oro

1. **`main` es la única verdad.** El portal y todos los despliegues siguen `main`. Nunca se trabaja "suelto".
2. **Siempre ramifica desde `main` actualizado.** Nunca desde otra rama de feature ni desde un estado viejo.
3. **Una tarea = una rama corta = un PR a `main`.** Ramas chicas, fáciles de revisar.
4. **Mantén tu rama al día con `main`.** Trae los cambios de `main` seguido para no divergir.
5. **Mergea por PR y BORRA la rama.** Una vez mergeado, la rama desaparece. Cero ramas eternas.
6. **Nunca commitees datos ni archivos locales** (`*.parquet`, `*.csv` de datos, `users.json`, resultados de simulación). Eso infla los PRs y genera conflictos.
7. **Ramas viven días, no semanas.** Si una rama lleva más de una semana abierta, algo va mal: ciérrala o pártela en pedazos.

---

## 🔄 El ciclo completo (copiar/pegar)

### 1. Empezar una tarea nueva — siempre desde `main` fresco
```bash
git checkout main
git pull origin main
git checkout -b feat/nombre-corto-de-la-tarea
```

### 2. Trabajar y guardar
```bash
git add .
git commit -m "feat: descripción clara de qué hace el cambio"
```
Haz varios commits chicos mientras avanzas. Está bien.

### 3. Subir la rama y abrir el PR (hacia `main`)
```bash
git push -u origin feat/nombre-corto-de-la-tarea
```
Luego en GitHub: **New Pull Request**, base = `main`, compare = tu rama. O por consola:
```bash
gh pr create --base main --fill
```

### 4. Antes de mergear — poner tu rama al día con `main`
Si `main` avanzó mientras trabajabas (otro mergeó algo), actualiza tu rama **en tu rama**
(nunca al revés):
```bash
git checkout feat/nombre-corto-de-la-tarea
git fetch origin
git merge origin/main          # resuelve aquí cualquier conflicto, en TU rama
git push
```
> Así los conflictos se resuelven en tu PR, y `main` se mantiene siempre limpio.

### 5. Mergear y limpiar
Una vez aprobado el PR, mergéalo en GitHub. **Después, borra la rama:**
```bash
git checkout main
git pull origin main
git branch -d feat/nombre-corto-de-la-tarea          # borra local
git push origin --delete feat/nombre-corto-de-la-tarea   # borra remota
```
(En GitHub también hay un botón **Delete branch** después del merge — úsalo.)

---

## ❌ Qué NO hacer

| No hagas esto | Por qué | Hazlo así |
|---|---|---|
| Trabajar directo en `main` | Rompes el portal de todos | Siempre una rama `feat/...` |
| Ramificar desde otra rama de feature | Arrastras cambios a medio hacer | Ramifica desde `main` actualizado |
| Dejar la rama abierta semanas | Diverge y entra en conflicto | Mergea o ciérrala en días |
| Commitear `*.csv` / `*.parquet` de datos | PR gigante + conflictos | Esos archivos van en `.gitignore` |
| `git push --force` a una rama compartida | Borras trabajo de otros | Nunca force-push a ramas que comparten |
| Resolver conflictos mergeando tu rama hacia `main` | Ensucias `main` | Trae `main` hacia tu rama y resuelve ahí |

---

## 🏷️ Convención de nombres de ramas

| Prefijo | Para qué | Ejemplo |
|---|---|---|
| `feat/` | Funcionalidad nueva | `feat/cumplimiento-comex` |
| `fix/` | Corrección de bug | `fix/export-excel-seek` |
| `chore/` | Mantenimiento, config, limpieza | `chore/actualizar-requirements` |

Nombre corto, en minúsculas, con guiones. Que se entienda de un vistazo.

---

## 🧹 Limpieza de ramas viejas (hacer una vez, y luego mantener)

Hoy hay 22 ramas acumuladas. Para ver cuáles ya se pueden borrar:
```bash
git fetch --prune origin
git branch -r --merged origin/main          # estas YA están en main → borrar sin miedo
```
Borrar una rama remota vieja:
```bash
git push origin --delete nombre-de-la-rama
```
> Las ramas que **no** están mergeadas pero ya no sirven (experimentos viejos),
> revísalas con el equipo antes de borrar — pueden tener trabajo que nadie rescató.

---

## 🛡️ Reglas en GitHub (recomendado activar)

Para que las reglas se cumplan solas, conviene activar **Branch protection** en `main`
(Settings → Branches → Add rule):

- ☑️ **Require a pull request before merging** → nadie puede pushear directo a `main`.
- ☑️ **Require branches to be up to date before merging** → obliga a actualizar con `main` antes de mergear.
- ☑️ **Delete head branches automatically** (Settings → General) → borra la rama sola al mergear.
- *(Opcional)* Require approvals → al menos 1 revisión antes de mergear.

Con eso, el flujo "desde main / hacia main / ramas cortas" deja de depender de la
disciplina de cada uno y lo fuerza GitHub.

---

## 🚀 En las máquinas de despliegue (portal en Perú)

Las PCs que **solo usan** el portal (no desarrollan) nunca deben crear ramas ni
commitear. Solo siguen `main`:
```bash
git fetch origin main
git reset --hard origin/main
```
(ver [GUIA-SOPORTE-PERU.md](GUIA-SOPORTE-PERU.md) para el detalle).

---

## 📌 Caso pendiente: PR #42

El PR #42 (`backup/wip-antes-reset` → `main`) está **en conflicto** y arrastra un
`data/inputs/compra.csv` de 36.000 líneas. Antes de mergearlo conviene:

1. Sacar del PR los archivos de **datos** (que no deberían versionarse).
2. Actualizar la rama con `main` y resolver conflictos **en la rama**.
3. Revisar que queden solo los cambios de **código** (los módulos del changelog).

Una vez limpio, se mergea y se borra la rama — y de ahí en adelante, todo el equipo
sigue este flujo para no volver a llegar a un PR así.

---

*El objetivo: que `main` siempre funcione, que los PRs sean chicos y revisables, y
que ninguna rama viva para siempre.*

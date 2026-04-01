# Flujo de Desarrollo

## Regla principal

**Nunca pushear directo a `main`.** Todo cambio entra vía Pull Request con al menos 1 aprobación.

## Pasos

1. Partir siempre desde `main` actualizado:
   ```bash
   git checkout main && git pull
   ```

2. Crear rama con prefijo descriptivo:
   ```bash
   git checkout -b feature/descripcion-corta
   # Prefijos: feature/, fix/, refactor/, docs/
   ```

3. Desarrollar, commitear con mensajes claros:
   ```bash
   git add archivos_modificados
   git commit -m "Descripción del cambio"
   ```

4. Push de la rama:
   ```bash
   git push -u origin feature/descripcion-corta
   ```

5. Abrir Pull Request a `main`:
   ```bash
   gh pr create --title "Título corto" --body "Descripción del cambio"
   ```

6. Esperar revisión y aprobación antes de merge.

## Convenciones

- PRs pequeños y enfocados (1 feature o fix por PR)
- Hacer `git pull origin main` frecuentemente para evitar conflictos
- Si hay conflictos, resolverlos en la rama antes del merge
- Usar Claude Code libremente en las ramas de desarrollo

# ADR-005: Fixture local de `auth.users` para validar el esquema

**Fecha:** 2026-09-08

## Contexto

La tabla `public.profiles` tiene una FK aprobada hacia `auth.users(id)`. Supabase Auth administra ese esquema en producción, pero una instancia PostgreSQL local estándar no lo incluye. Esto impedía validar las migraciones Alembic contra una base local desechable.

## Decisión tomada

Se autoriza el fixture mínimo `scripts/local-dev/auth_stub.sql`, que crea exclusivamente `auth.users(id uuid primary key default gen_random_uuid())` cuando se valida localmente. El fixture vive fuera de `sql/`, fuera de `backend/migrations/versions/` y fuera de toda cadena ejecutable de Alembic.

El fixture nunca se aplica a Supabase ni a otro host no local. La guardia del runbook exige confirmar que el host de `DATABASE_MIGRATION_URL` es `localhost` o `127.0.0.1` antes de ejecutarlo.

## Consecuencias

- Alembic conserva las mismas migraciones que se desplegarán en Supabase y no descubre el fixture como una revisión.
- El fixture no intenta simular Supabase Auth: no incorpora columnas, políticas, triggers ni lógica adicional.
- Cualquier ampliación del stub o uso fuera de validación local requiere una decisión y autorización independientes.

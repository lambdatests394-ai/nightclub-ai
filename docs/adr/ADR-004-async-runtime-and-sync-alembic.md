# ADR-004: Runtime asíncrono y migraciones Alembic síncronas

**Fecha:** 2026-09-07

## Contexto

Night Club AI usa FastAPI y SQLAlchemy para operaciones de aplicación que hacen E/S contra PostgreSQL. Alembic, en cambio, ejecuta DDL de despliegue y necesita una conexión estable, separada y explícita para la revisión de esquema. Mezclar el motor asíncrono de runtime con el contexto síncrono de Alembic haría ambigua la responsabilidad de cada camino de conexión.

## Decisión tomada

El runtime de la aplicación usa SQLAlchemy asíncrono con el controlador `asyncpg`, a partir de `DATABASE_URL`. Alembic usa SQLAlchemy síncrono con `psycopg`, a partir de `DATABASE_MIGRATION_URL` (o `DATABASE_URL` normalizada a `postgresql+psycopg` cuando corresponda).

La decisión es intencional: E/S asíncrona, lógica pura síncrona. Los repositorios, proveedores de red y servicios/endpoints que esperan E/S usan `async def`; la evaluación de políticas, permisos y reglas puras no requiere corutinas. Los scripts de migración permanecen síncronos y no reutilizan el `AsyncEngine` ni la sesión de runtime.

**Aclaración aprobada, 2026-09-08 (checkpoint humano de Prompt 4):** esta regla sustituye la obligación anterior de hacer asíncrona toda lógica pura. El código existente de `state_machine.py` y `workflow_service.py` conserva su interfaz de Prompt 3; no se autoriza refactorizar contenido en Prompt 4. La corrección de este ADR es documental.

## Consecuencias

- Los adaptadores de persistencia de aplicación deberán usar `AsyncSession` y sus métodos se esperarán con `await`.
- Las futuras rutas FastAPI y servicios de dominio que intervengan en una operación de aplicación deberán conservar la cadena asíncrona, sin llamadas bloqueantes a `psycopg`.
- Las migraciones usarán únicamente el motor síncrono configurado en `backend/migrations/env.py`; este camino queda reservado para DDL y mantenimiento, no para lógica de negocio.
- Las nuevas políticas puras se prueban directamente como funciones síncronas. Las pruebas de interfaces asíncronas existentes siguen esperando sus corutinas explícitamente; la lógica de migración continúa síncrona.

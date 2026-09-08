# ADR-004: Runtime asíncrono y migraciones Alembic síncronas

**Fecha:** 2026-09-07

## Contexto

Night Club AI usa FastAPI y SQLAlchemy para operaciones de aplicación que hacen E/S contra PostgreSQL. Alembic, en cambio, ejecuta DDL de despliegue y necesita una conexión estable, separada y explícita para la revisión de esquema. Mezclar el motor asíncrono de runtime con el contexto síncrono de Alembic haría ambigua la responsabilidad de cada camino de conexión.

## Decisión tomada

El runtime de la aplicación usa SQLAlchemy asíncrono con el controlador `asyncpg`, a partir de `DATABASE_URL`. Alembic usa SQLAlchemy síncrono con `psycopg`, a partir de `DATABASE_MIGRATION_URL` (o `DATABASE_URL` normalizada a `postgresql+psycopg` cuando corresponda).

La decisión es intencional: los servicios de dominio que participan en flujos de aplicación y los futuros endpoints FastAPI se exponen como `async def`; `state_machine.py` y `workflow_service.py` siguen esa convención aunque la máquina de estados hoy no haga E/S. Los scripts de migración permanecen síncronos y no reutilizan el `AsyncEngine` ni la sesión de runtime.

## Consecuencias

- Los adaptadores de persistencia de aplicación deberán usar `AsyncSession` y sus métodos se esperarán con `await`.
- Las futuras rutas FastAPI y servicios de dominio que intervengan en una operación de aplicación deberán conservar la cadena asíncrona, sin llamadas bloqueantes a `psycopg`.
- Las migraciones usarán únicamente el motor síncrono configurado en `backend/migrations/env.py`; este camino queda reservado para DDL y mantenimiento, no para lógica de negocio.
- Pruebas de lógica pura pueden ejecutar corutinas de forma controlada, sin convertir la lógica de migración en asíncrona.

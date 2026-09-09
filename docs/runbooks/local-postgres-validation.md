# Validación local de Alembic con PostgreSQL

## Propósito

Validar las migraciones de Night Club AI contra una base PostgreSQL local y desechable. Este runbook no conecta con Supabase y nunca ejecuta `sql/003_rls.sql`.

## Prerrequisitos

- PostgreSQL local en ejecución.
- Un rol local de pruebas con permiso para crear y eliminar la base desechable.
- Entorno virtual instalado.
- La URL se configura solo en la sesión actual; no se guarda en `.env` ni en Git.

## Flujo

1. Configure temporalmente la URL de la base de pruebas:

   ```powershell
   $env:DATABASE_MIGRATION_URL = "postgresql+psycopg://alembic_test_user:<password>@127.0.0.1:5432/nightclub_ai_alembic_test"
   ```

2. Antes de aplicar el fixture, compruebe el host. Debe imprimir exactamente `localhost` o `127.0.0.1`; ante cualquier otro valor, deténgase.

   ```powershell
   $validationUri = [System.Uri]$env:DATABASE_MIGRATION_URL.Replace("postgresql+psycopg://", "postgresql://")
   $validationUri.Host
   if ($validationUri.Host -notin @("localhost", "127.0.0.1")) { throw "auth_stub.sql solo puede ejecutarse en PostgreSQL local." }
   ```

3. Cree o recree la base desechable, conectándose a la base local `postgres` como el rol de pruebas:

   ```sql
   DROP DATABASE IF EXISTS nightclub_ai_alembic_test;
   CREATE DATABASE nightclub_ai_alembic_test OWNER alembic_test_user;
   ```

4. Aplique el fixture local mínimo. Este paso no pertenece a Alembic y no se debe ejecutar contra Supabase:

   ```powershell
   psql --host 127.0.0.1 --username alembic_test_user --dbname nightclub_ai_alembic_test --file scripts/local-dev/auth_stub.sql
   ```

5. Ejecute la validación de migraciones y la suite unitaria:

   ```powershell
   .\.venv\Scripts\alembic.exe upgrade head
   .\.venv\Scripts\alembic.exe downgrade 20260907_0001
   .\.venv\Scripts\alembic.exe upgrade head
   .\.venv\Scripts\python.exe -m pytest backend/tests -q
   ```

   Entre los comandos de Alembic, consulte el catálogo de PostgreSQL para verificar tablas, enums, FKs, triggers e índices; incluya el índice parcial `uq_publication_jobs_one_active_job_per_content_item`.

6. Limpie la base de pruebas al terminar y borre la variable de la sesión:

   ```sql
   DROP DATABASE nightclub_ai_alembic_test;
   ```

   ```powershell
   Remove-Item Env:\DATABASE_MIGRATION_URL
   ```

## Límites de seguridad

- `scripts/local-dev/auth_stub.sql` es exclusivamente un fixture local.
- Nunca aplique el fixture, estas migraciones de validación ni `sql/003_rls.sql` a Supabase desde este flujo.
- No registre contraseñas en comandos compartidos, archivos, commits ni salidas de CI.

## Prompt 4 — integración local reutilizable

El checkpoint humano autoriza únicamente `nightclub_ai_prompt4_test`, en `127.0.0.1` o `localhost:5432`, con `alembic_test_user`. El rol se conserva: LOGIN/CREATEDB verdaderos; SUPERUSER/CREATEROLE/REPLICATION/BYPASSRLS falsos. Nunca se usa como `DATABASE_URL` de FastAPI.

Desde la raíz, configure `DATABASE_MIGRATION_URL` solo en la sesión con la contraseña local suministrada por el owner (URL-encode si contiene caracteres reservados). El siguiente texto contiene un placeholder, no una credencial:

```powershell
$env:DATABASE_MIGRATION_URL = "postgresql+psycopg://alembic_test_user:<password>@127.0.0.1:5432/nightclub_ai_prompt4_test"
try {
    .\.venv\Scripts\python.exe -u scripts/local-dev/validate_prompt4.py
    if ($LASTEXITCODE -ne 0) { throw "La validación local falló; revise la salida y la limpieza." }
} finally {
    Remove-Item Env:\DATABASE_MIGRATION_URL -ErrorAction SilentlyContinue
}
```

El harness valida e imprime host/atributos del rol sin URL ni contraseña; rechaza otra base/rol/puerto/driver y rechaza una base ya existente. Después crea su base, aplica el fixture mínimo aprobado **fuera de Alembic**, ejecuta `alembic upgrade head` y `pytest backend/tests --local-postgres -q`, consulta la revisión real y elimina la base en `finally`. No fuerza cierres de sesiones, no borra el rol y comprueba su conservación. Una caída abrupta del proceso puede impedir `finally`: inspeccione catálogo/sesiones y solicite limpieza explícita antes de reintentar si la base permanece.

Las pruebas inyectan un motor asyncpg de alcance exclusivo al fixture, derivado de la URL de migración validada; no asignan el rol de validación al runtime. Cada caso usa transacción externa y savepoints, rollback y cierre del motor. Sin `--local-postgres`, estas pruebas se omiten explícitamente y la suite offline sigue siendo ejecutable.

No se crea nueva migración, no se amplía `auth_stub.sql`, no se ejecuta `sql/003_rls.sql`, no se conecta a Supabase. El harness de Prompt 4 no sustituye el ciclo upgrade/downgrade/upgrade y verificación de catálogo de Prompt 3: valida las migraciones existentes y la integración identity aprobada.

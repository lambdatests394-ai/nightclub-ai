# Validación local de Alembic con PostgreSQL

## Propósito

Validar las migraciones de Night Club AI contra una base PostgreSQL local y desechable. Este runbook no conecta con Supabase y nunca ejecuta `sql/003_rls.sql`.

## Prerrequisitos

- PostgreSQL local en ejecución.
- Un rol local de pruebas con permiso para crear y eliminar la base desechable.
- Entorno virtual instalado.
- La URL se configura solo en la sesión actual; no se guarda en `.env` ni en Git.

## Flujo histórico de Prompt 3 (revisiones 0001/0002)

No use `head` para este ciclo histórico: 0003 bloquea el downgrade de seguridad.

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
   .\.venv\Scripts\alembic.exe upgrade 20260907_0002
   .\.venv\Scripts\alembic.exe downgrade 20260907_0001
   .\.venv\Scripts\alembic.exe upgrade 20260907_0002
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

El harness valida e imprime host/atributos del rol sin URL ni contraseña; rechaza otra base/rol/puerto/driver y rechaza una base ya existente. Después crea su base, aplica el fixture mínimo aprobado **fuera de Alembic**, ejecuta `alembic upgrade 20260907_0002` y `pytest backend/tests --local-postgres -q`, consulta la revisión real y elimina la base en `finally`. No fuerza cierres de sesiones, no borra el rol y comprueba su conservación. Una caída abrupta del proceso puede impedir `finally`: inspeccione catálogo/sesiones y solicite limpieza explícita antes de reintentar si la base permanece.

Las pruebas inyectan un motor asyncpg de alcance exclusivo al fixture, derivado de la URL de migración validada; no asignan el rol de validación al runtime. Cada caso usa transacción externa y savepoints, rollback y cierre del motor. Sin `--local-postgres`, estas pruebas se omiten explícitamente y la suite offline sigue siendo ejecutable.

No se crea nueva migración, no se amplía `auth_stub.sql`, no se ejecuta `sql/003_rls.sql`, no se conecta a Supabase. El harness de Prompt 4 no sustituye el ciclo upgrade/downgrade/upgrade y verificación de catálogo de Prompt 3: valida las migraciones existentes y la integración identity aprobada.

## Prompt 5 — validación RLS aprobada y rol local conservado

Estado actual: la provisión local y la validación PostgreSQL/RLS de Stage B están
completadas y aprobadas por revisión humana. Resultado real confirmado: 301 passed /
12 skips históricos esperados; 0002 -> 0003, tres políticas bootstrap, 20/20 tablas
con ENABLE/FORCE, downgrade inseguro bloqueado y base desechable eliminada.
No repetir la provisión. Nunca elevar privilegios de `alembic_test_user`, que
conserva CREATEDB/NOCREATEROLE/NOSUPERUSER/NOBYPASSRLS.

La migración usa el nombre fijo `nightclub_api`, ya provisionado LOCALMENTE con
credencial independiente. El rol se conserva salvo una futura decisión explícita
de ciclo de vida; no se elimina como limpieza de Prompt 5. No es una credencial de
runtime de despliegue y no se reutiliza en Supabase/Railway.

### Referencia histórica de provisión — completada, no repetir

Los comandos siguientes documentan la provisión inicial ya completada, no son
instrucciones activas para este checkpoint. El ejemplo usó postgres únicamente
como identidad administrativa interactiva del owner; no volver a ejecutarlos:

```powershell
& 'C:\Program Files\PostgreSQL\16\bin\psql.exe' -X --host=127.0.0.1 --port=5432 --username=postgres --dbname=postgres --set=ON_ERROR_STOP=1
```

Dentro de psql, comprobar primero que no exista el rol:

```sql
SELECT rolname FROM pg_roles WHERE rolname = 'nightclub_api';
```

Solo si devuelve cero filas, ejecutar (sin contraseña en SQL):

```sql
CREATE ROLE nightclub_api LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreatedb,
       rolcreaterole, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname = 'nightclub_api';
```

Asignar la contraseña únicamente mediante el prompt interactivo seguro de psql:

```text
\password nightclub_api
\q
```

No compartir contraseñas por chat ni almacenarlas en archivos. El owner confirmó
que la provisión terminó y los atributos coinciden; la posterior validación RLS
real también pasó. No ejecutar sql/003. Las pruebas unitarias no sustituyen la
evidencia real de PostgreSQL ya aprobada.

### Flujo reutilizable de validación local

Este flujo ya pasó el checkpoint de Prompt 5. Se conserva para futuras ejecuciones
autorizadas; la higiene documental no requiere repetir la validación destructiva.

El owner inyecta en la sesión, sin imprimir sus valores ni guardarlos en .env:

- `DATABASE_MIGRATION_URL`: alembic_test_user, loopback:5432, base nightclub_ai_prompt5_test, driver psycopg.
- `PROMPT5_RUNTIME_URL`: nightclub_api local conservado, misma base/host/puerto, driver asyncpg o psycopg.

Ambas URLs requieren contraseña no vacía, escapada para URL cuando corresponda.
No se muestra aquí una URL con credenciales. Ejecutar desde la raíz:

```powershell
try {
    .\.venv\Scripts\python.exe -u scripts/local-dev/validate_prompt5.py
    if ($LASTEXITCODE -ne 0) { throw 'Validación RLS fallida o bloqueada; no reintentar a ciegas.' }
} finally {
    Remove-Item Env:\DATABASE_MIGRATION_URL -ErrorAction SilentlyContinue
    Remove-Item Env:\PROMPT5_RUNTIME_URL -ErrorAction SilentlyContinue
}
```

El harness rechaza base preexistente, comprueba roles sin crearlos, crea solo
nightclub_ai_prompt5_test, imprime hosts, aplica el stub sin cambios, comprueba
login runtime, migra hasta 0002, siembra fixtures y aplica 0003. Ejecuta la suite
con `--prompt5-postgres`, verifica rechazo del downgrade y vuelve a comprobar
catálogo/HEAD sin intentar un downgrade destructivo. Los 12 tests históricos de
Prompt 4 quedan omitidos en esta base; tienen su harness 0002 separado.

Los fixtures incluyen perfil/organización inactivos y membresía previamente
eliminada. No se añaden políticas para que el propietario pueda saltarse FORCE.
La prueba de aislamiento usa SELECT sin WHERE con el runtime no propietario.
Los tests de las otras 17 tablas prueban denegación por privilegios, no una
expresión RLS de negocio que deliberadamente no existe todavía.

### Limpieza

El harness cierra motores y borra solo la base que creó si no quedan sesiones.
No fuerza terminaciones. Verifica su ausencia y conserva alembic_test_user. Si hay
sesiones o el proceso cae, detenerse e inspeccionar antes de una limpieza manual.

Conservar `nightclub_api` localmente salvo una futura decisión explícita de ciclo
de vida. No eliminarlo automáticamente ni como limpieza de Prompt 5. Conservar
también `alembic_test_user` con sus atributos aprobados, sin cambios de privilegios
ni credenciales. La limpieza de la base desechable ya fue confirmada con
`TEST_DATABASE_REMAINING: []`; eliminar el rol no es un requisito de cierre.

## Prompt 6 — harness independiente (validación real completada)

Prompt 5 está cerrado. Su harness ahora fija `upgrade 20260909_0003`, nunca `head`.
Sus expectativas históricas de tres políticas y privilegios no se cambian para
pasar en 0004. Prompt 6 utiliza exclusivamente `nightclub_ai_prompt6_test`.

Requisitos: PostgreSQL local existente, `.venv` y dependencias existentes. Usar
los roles ya provisionados `alembic_test_user` y `nightclub_api`. No recrearlos,
alterarlos, cambiar contraseñas, ampliar poderes ni eliminarlos.

En la misma PowerShell del propietario, configurar de forma privada estas
variables de sesión (nunca guardar valores en `.env`, archivos, chat o historial):

| Variable | Destino requerido, sin mostrar credenciales |
| --- | --- |
| DATABASE_MIGRATION_URL | postgresql+psycopg, alembic_test_user, 127.0.0.1:5432, nightclub_ai_prompt6_test |
| PROMPT6_RUNTIME_URL | postgresql+asyncpg, nightclub_api, 127.0.0.1:5432, nightclub_ai_prompt6_test |
| DATABASE_RUNTIME_EXPECTED_ROLE | nightclub_api (el harness también lo fija para sus hijos) |

`localhost` también está permitido, pero usar el mismo hostname explícito en
ambas URLs. Contraseñas con caracteres reservados deben estar codificadas en la
URL. No imprimir variables, DSNs ni errores de conexión que puedan contenerlos.
Este proceso Codex no hereda automáticamente otra sesión PowerShell del usuario.

Desde la raíz `nightclub-ai` del repositorio actual, ejecutar en esa misma sesión:

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_prompt6.py
$LASTEXITCODE
```

No es necesario crear previamente la base. El harness valida los dos destinos
antes de conectarse; rechaza bases existentes, hosts no locales, otro puerto,
parámetros adicionales y roles inesperados. Crea solo la base indicada, aplica
el auth_stub congelado local, migra a 0002, siembra identidad local, migra a 0003,
captura las tres políticas, migra a `20260910_0004` y verifica que no cambiaron.
Ejecuta la suite con `--prompt6-postgres`, verifica el downgrade bloqueado y
repite únicamente el catálogo después del rechazo. No reintenta fallos.

PASS requiere exit 0, pruebas sin fallos, `BOOTSTRAP_UNCHANGED: PASS`, 10 políticas,
`ALEMBIC_HEAD: 20260910_0004`, pruebas de privilegios/20 FORCE/RLS, downgrade
rechazado con su marcador exacto, `TEST_DATABASE_REMAINING: []` y los dos mensajes
`ROLE_RETAINED_UNCHANGED`. Los 55 tests históricos opt-in se omiten aquí.
Un fallo de comando/invariante implica detenerse y revisar, no reparar en silencio.

En `finally`, cierra conexiones y elimina exclusivamente la base que creó, sin
terminar sesiones ajenas. Si quedan sesiones, reporta limpieza bloqueada; no
intenta borrar roles ni eliminar una base preexistente. Elimina sus variables
de proceso; la PowerShell padre requiere limpieza independiente:

```powershell
Remove-Item Env:\DATABASE_MIGRATION_URL -ErrorAction SilentlyContinue
Remove-Item Env:\PROMPT6_RUNTIME_URL -ErrorAction SilentlyContinue
```

En una futura repetición sin variables disponibles, ejecutar solo pruebas sin
opt-in y reportar esa repetición como PENDING; esto no invalida el checkpoint real
ya aprobado. No solicitar contraseñas por chat. Nunca ejecutar el SQL histórico 003,
contactar Supabase ni reutilizar credenciales de infraestructura ajena.

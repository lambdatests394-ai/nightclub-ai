# Prompt 4 — Stage B: informe de entrega

Fecha: 2026-09-08. Implementación y validación local terminadas; **pendiente de revisión humana, sin commit ni merge**. El checkpoint humano aprobó Stage B y sus aclaraciones vinculantes. No se declara aprobación humana de esta entrega.

## 1. Arquitectura implementada

Bearer JWT -> verificador ES256 y JWKS inyectable -> CurrentUser -> perfil activo -> repositorio async acotado por membresía -> OrganizationContext -> RBAC central -> tres endpoints GET. E/S async, políticas puras sync. Contrato completo en [PROMPT_4_SECURITY.md](PROMPT_4_SECURITY.md).

## 2. Archivos creados/modificados

Modificados (10):

- `.env.example`: tres parámetros de seguridad no secretos; issuer/audience/JWKS siguen vacíos.
- `README.md`: alcance actual y enlaces de validación.
- `backend/app/core/config.py`: configuración validada de JWT/JWKS.
- `backend/app/main.py`: factory/lifespan, composición, errores seguros, correlación/logging y rutas identity; health conserva su contrato.
- `backend/app/modules/identity/models.py`: solo alineación de member_role y last_seen_at.
- `backend/app/modules/identity/schemas.py`: envelopes, metadatos, paginación y membresía de lectura.
- `docs/adr/ADR-001-fastapi-postgresql-role-and-rls.md`: aclaración del control actual vs. RLS futuro.
- `docs/adr/ADR-004-async-runtime-and-sync-alembic.md`: async para E/S, sync para políticas puras; contenido no refactorizado.
- `docs/runbooks/local-postgres-validation.md`: flujo local opt-in de Prompt 4.
- `requirements.txt`: dos dependencias directas aprobadas; eliminación de espacios finales en línea existente.

Creados (17):

- `backend/app/api/v1/identity.py`: los tres endpoints protegidos.
- `backend/app/modules/identity/authentication.py`: verificación criptográfica y contrato de claims.
- `backend/app/modules/identity/dependencies.py`: composición y sesión con propagación de errores/rollback.
- `backend/app/modules/identity/errors.py`: errores públicos seguros.
- `backend/app/modules/identity/jwks.py`: proveedor HTTP y caché acotada.
- `backend/app/modules/identity/policy.py`: valores inmutables, roles, permiso y política.
- `backend/app/modules/identity/repository.py`: persistencia async filtrada.
- `backend/app/modules/identity/service.py`: perfil activo, contexto y paginación.
- `backend/tests/conftest.py`: material criptográfico efímero, reloj y proveedor locales; configuración AnyIO/opt-in.
- `backend/tests/test_identity_access.py`: políticas, contexto, HTTP y aislamiento.
- `backend/tests/test_identity_composition.py`: sesión, cleanup/lifespan y variables de entorno.
- `backend/tests/test_identity_jwks.py`: caché, rotación real de clave, concurrencia y fallos.
- `backend/tests/test_identity_jwt.py`: matriz de ataques y claims.
- `backend/tests/test_identity_postgres.py`: 12 casos contra PostgreSQL real.
- `docs/PROMPT_4_SECURITY.md`: contrato de seguridad y límites.
- `docs/PROMPT_4_IMPLEMENTATION_REPORT.md`: este informe.
- `scripts/local-dev/validate_prompt4.py`: harness local con guardias y limpieza.

## 3. Roles reales

`owner`, `manager`, `editor`, `reviewer`, `operator`, `viewer`: mismos seis valores del enum PostgreSQL existente, probados individualmente mediante persistencia real.

## 4. Modelo de estado de membresía

No existe columna status ni membership_id. Se exige fila exacta usuario/organización, perfil y organización existentes/activos, rol reconocido. No se inventaron columnas o estados.

## 5. Mecanismo JWT

PyJWT con cryptography, clave pública EC P-256 de JWKS confiable. Requiere iss/aud/exp/iat/sub/role, UUID válido, role authenticated, rechazo de anon/service_role/is_anonymous=true, verificación de nbf opcional y NumericDate finito. Ningún claim no verificado llega al repositorio.

## 6. Algoritmo permitido

Solo `SUPABASE_JWT_ALLOWED_ALGORITHMS=["ES256"]`. HS256, RS256, none o ampliaciones fallan; header alg no modifica configuración. Probados alg:none, firma inválida y confusión HMAC con clave pública.

## 7. Issuer/audience

Issuer exacto configurado. Una audiencia esperada, aceptando representación JWT string o array válida mediante PyJWT. Sin valores derivados del token, prefijos ni fallback con configuración vacía.

## 8. Clock skew

60 segundos explícitos para exp/iat/nbf. Casos de límites y tipos malformados probados con reloj controlado.

## 9. Variables existentes reutilizadas

`SUPABASE_JWT_ISSUER`, `SUPABASE_JWT_AUDIENCE`, `SUPABASE_JWKS_URL`. Persistencia conserva DATABASE_URL para runtime y DATABASE_MIGRATION_URL para migraciones/validación local. Sin valores reales en archivos.

## 10. Variables añadidas

`SUPABASE_JWT_ALLOWED_ALGORITHMS=["ES256"]`, `JWT_CLOCK_SKEW_SECONDS=60`, `SUPABASE_JWKS_CACHE_TTL_SECONDS=300`. Lectura desde entorno probada.

## 11. Abstracción JWKS

Protocolo async e inyectable; adaptador httpx TLS, HTTPS confiable, sin redirects, sin URLs del JWT y deadline de 5 s. Client creado/cerrado por lifespan, sin descarga en startup. Pruebas locales sin red externa.

## 12. Caché en memoria

Por proceso, reloj monotónico, lock async, refresh atómico. Máximo 32 claves/256 KiB; ningún almacenamiento persistente añadido.

## 13. TTL

Default 300 s, máximo configurable 600 s. Expiración exige nuevo material, fallos no prolongan TTL. Controla únicamente la caché local: no promete revocación global en 300 s por caches upstream.

## 14. Kid desconocido y rotación

Máximo un refresh por intento; intervalo compartido mínimo de 30 s; concurrencia comparte snapshot. Kid ausente tras snapshot confiable: 401. Material requerido no obtenible: 503. Prueba rota a una nueva clave privada EC efímera y comprueba rechazo de la clave retirada después del refresh.

## 15. Fail-closed

No hay omisión de firma ni aceptación optimista. Clave conocida no vencida puede verificar durante caída del proveedor. Sin material confiable vigente, la operación protegida no llega al repositorio.

## 16. Mapeo HTTP

401: credencial/JWT inválido o kid ausente en snapshot confiable, con WWW-Authenticate: Bearer. 403: perfil/membresía/organización/permiso/selector denegado; organización inexistente/inactiva/ajena recibe misma respuesta genérica. 503: configuración o infraestructura de verificación requerida no disponible; persistencia indisponible usa IDENTITY_UNAVAILABLE. Parámetros malformados: 422 seguro. Correlación sin exponer inputs/SQL/excepciones.

## 17. CurrentUser

Dataclass frozen con solo user_id UUID. No JWT, Authorization, email ni rol organizacional.

## 18. OrganizationContext

Dataclass frozen con organization_id, user_id, role; solo producido tras validar membresía exacta y entidades activas. Sin campos de membresía inventados.

## 19. Matriz RBAC exacta

| Rol | organization:read tras membresía válida | Otro permiso |
| --- | --- | --- |
| owner | Sí | No |
| manager | Sí | No |
| editor | Sí | No |
| reviewer | Sí | No |
| operator | Sí | No |
| viewer | Sí | No |

Rol desconocido: denegado. Ningún owner global. Los endpoints personales requieren usuario/perfil activo, no un rol organizacional.

## 20. Aislamiento tenant

Queries por usuario verificado y organización exacta, flags activos y enum reconocido; listados filtran antes de paginar. Contexto vuelve a comprobar IDs/rol. Path canónico; header de organización opcional debe coincidir. Tests incluyen suplantación por query/body/header, organización ajena/inexistente, perfil inactivo y eliminación de membresía. RLS tenant-aware no está activa ni se reclama como protección actual.

## 21. Endpoints implementados

GET `/api/v1/me`, GET `/api/v1/me/organizations`, GET `/api/v1/organizations/{organization_id}`. CamelCase, `{data, meta}`, correlationId, no-store. Listado con limit 1–100/default 50 y cursor de posición, nunca autoridad. Ninguna mutación ni ruta de otro módulo añadida.

## 22. Pruebas añadidas

163 casos adicionales: JWT/claims/algoritmos/tiempo, JWKS/cache/fallos/rotación, seis roles/default deny, contexto/tenant isolation, contratos HTTP/errores/redacción, composición/configuración y 12 casos de PostgreSQL. Las 48 pruebas previas permanecen sin regresión. Plugin AnyIO con asyncio; las corutinas se esperan explícitamente.

## 23. Resultado completo de pytest

Comando de la ejecución final: `python -m pytest backend/tests --local-postgres -q`. Salida de pytest; únicamente las rutas absolutas de la máquina se sustituyen por `<workspace>` para mantener portable este documento:

```text
........................................................................ [ 34%]
........................................................................ [ 68%]
...................................................................      [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  <workspace>\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv\Lib\site-packages\_pytest\cacheprovider.py:469
  <workspace>\.venv\Lib\site-packages\_pytest\cacheprovider.py:469: PytestCacheWarning: could not create cache path <workspace>\.pytest_cache\v\cache\nodeids: [WinError 5] Acceso denegado: '<workspace>\\.pytest_cache\\v\\cache'
    config.cache.set("cache/nodeids", sorted(self.cached_nodeids))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
211 passed, 2 warnings in 6.92s
```

También se ejecutó la suite sin opt-in: `199 passed, 12 skipped, 1 warning in 4.68s`. Los 12 skips pertenecen exclusivamente a integración, todos ejecutados en la corrida real final. No se silenció la advertencia de Starlette ni se añadió httpx2; no se cambiaron permisos de caché del usuario.

## 24. PostgreSQL real y limpieza

Harness ejecutado desde la raíz con variable de sesión temporal; host impreso antes del fixture. Salida relevante literal (se omiten solo líneas RUN con ruta absoluta al intérprete):

```text
VALIDATED_HOST: 127.0.0.1
VALIDATION_ROLE: ('alembic_test_user', False, False, True, True, False, False)
CREATE DATABASE nightclub_ai_prompt4_test: OK
APPLY LOCAL auth_stub.sql: OK
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 20260907_0001, Create the approved Night Club AI v1.0 schema.
INFO  [alembic.runtime.migration] Running upgrade 20260907_0001 -> 20260907_0002, Create the approved Night Club AI v1.0 indexes.
ALEMBIC_HEAD: ('20260907_0002',)
TEST_DATABASE_REMAINING: []
ROLE_RETAINED: ('alembic_test_user', True)
VALIDATION_PROCESS_MIGRATION_URL_REMOVED
SESSION_MIGRATION_URL_PRESENT: False
```

Los 12 tests reales verificaron persistencia de cada rol con asyncpg, last_seen_at timezone-aware, FK externa de profiles, rechazo de rol inválido, scopes y paginación, inactivación/eliminación y HTTP con repositorio real. No se ejecutó downgrade ni sql/003 en Prompt 4: no hay migración nueva que validar; se aplicaron las revisiones congeladas existentes sobre base vacía. La base creada se eliminó; **el rol reutilizable se conservó** y no se tocó el servicio PostgreSQL instalado.

## 25. Compileall

`python -m compileall backend scripts/local-dev/validate_prompt4.py`: PASS, sin errores de compilación. Se compilaron los tests nuevos y el harness además del backend.

## 26. Herramientas estáticas/dependencias

No hay framework de lint/type checking configurado en este proyecto; no se añadió uno. `python -m pip check`: `No broken requirements found.` `git diff --check`: sin errores de whitespace tras retirar espacios finales de requirements; Git avisa de conversión futura LF/CRLF según su configuración existente, no modificada.

## 27. Dependencias añadidas

`PyJWT[crypto]==2.13.0`: firma/validación JWT con primitivas criptográficas probadas. `httpx==0.28.1`: dependencia directa explícita del transporte JWKS async y pruebas HTTP. Instalación ejecutada en el entorno virtual y pip check correcto; requirements.txt sigue siendo la única fuente de dependencias. Sin Redis/framework auth ni pyproject.toml.

## 28. Migraciones creadas

**NINGUNA**. SQL 001/002/003, Alembic 0001/0002 y auth_stub.sql sin cambios; comparación con fd4045b verificada. Sin ejecución de sql/003.

## 29. ADRs y documentación

Actualizados ADR-001 y ADR-004 con las aclaraciones humanas ya aprobadas; README y runbook. Creados contrato de seguridad y este informe. No se adoptó otra decisión arquitectónica consecuencial.

## 30. Supuestos de seguridad y escaneo

Se confía en la configuración del operador, TLS y reloj correcto. El rol runtime previsto sigue siendo nightclub_api, nunca el rol local o service_role. Claves/token de pruebas efímeros, generados en memoria. Escaneo de archivos cambiados/nuevos: cero coincidencias de credenciales conocidas de la sesión, claves privadas, tokens GitHub/OpenAI o JWT persistidos; dos candidatos URL en el runbook, ambos placeholders `<password>`, revisados. Los nombres SECRET/KEY de configuración y pruebas negativas no son credenciales reales. Este escaneo no equivale a una auditoría externa completa.

## 31. Riesgos restantes

RLS tenant-aware futura, caches upstream y revocación de JWT no inmediata, rate limiting/operación multiproceso pendientes; perfil/membresía se leen por solicitud, no hay revocación retroactiva de una lectura concurrente. Health ready no comprueba DB/JWKS. Access logs externos requieren redacción apropiada. Integración con Supabase real deliberadamente no probada. Advertencias conocidas de Starlette y cache pytest sin impacto en los asserts; no se ocultaron.

## 32. Trabajo diferido

Revisión humana y autorización separada de commit. No login/provisión/refresh, permisos de negocio, RLS, despliegue, Meta, WhatsApp, n8n ni Prompt 5. El contrato futuro de RLS debe cubrir contexto transaccional, bootstrap de membresías y aislamiento del pool; no se implementó.

## 33. Git diff --stat

Salida para archivos **ya versionados**; Git no incluye los 17 nuevos sin stage en esta estadística. La lista completa está en el punto 2.

```text
 .env.example                                       |   3 +
 README.md                                          |  10 +-
 backend/app/core/config.py                         |  33 +++++++
 backend/app/main.py                                | 104 ++++++++++++++++++---
 backend/app/modules/identity/models.py             |  13 ++-
 backend/app/modules/identity/schemas.py            |  26 ++++++
 .../adr/ADR-001-fastapi-postgresql-role-and-rls.md |   8 ++
 docs/adr/ADR-004-async-runtime-and-sync-alembic.md |   6 +-
 docs/runbooks/local-postgres-validation.md         |  22 +++++
 requirements.txt                                   |   4 +-
 10 files changed, 208 insertions(+), 21 deletions(-)
```

## 34. Git status

Cambios sin stage, sin commit. Working tree intencionalmente NO clean: contiene la entrega pendiente de revisión.

```text
 M .env.example
 M README.md
 M backend/app/core/config.py
 M backend/app/main.py
 M backend/app/modules/identity/models.py
 M backend/app/modules/identity/schemas.py
 M docs/adr/ADR-001-fastapi-postgresql-role-and-rls.md
 M docs/adr/ADR-004-async-runtime-and-sync-alembic.md
 M docs/runbooks/local-postgres-validation.md
 M requirements.txt
?? backend/app/api/v1/identity.py
?? backend/app/modules/identity/authentication.py
?? backend/app/modules/identity/dependencies.py
?? backend/app/modules/identity/errors.py
?? backend/app/modules/identity/jwks.py
?? backend/app/modules/identity/policy.py
?? backend/app/modules/identity/repository.py
?? backend/app/modules/identity/service.py
?? backend/tests/conftest.py
?? backend/tests/test_identity_access.py
?? backend/tests/test_identity_composition.py
?? backend/tests/test_identity_jwks.py
?? backend/tests/test_identity_jwt.py
?? backend/tests/test_identity_postgres.py
?? docs/PROMPT_4_IMPLEMENTATION_REPORT.md
?? docs/PROMPT_4_SECURITY.md
?? scripts/local-dev/validate_prompt4.py
```

## 35. Rama activa

`feature/prompt-4-identity-auth`, HEAD fd4045ba5e3a5051045509cfc8ca5d5159516355, sin commit nuevo. main/develop permanecen en ese mismo hash; el objeto del tag anotado prompt-3-validated sigue en 850a61cb04eee4455a04417b3c5501b0e70129b3.

## 36. Confirmaciones de alcance

Supabase remoto no contactado. sql/003_rls.sql no ejecutado. main, develop y prompt-3-validated no modificados. Sin merge, commit, push ni cambio de remoto. SQL/migraciones congeladas, state machine, workflow service y sus tests intactos. No se implementó Meta/WhatsApp/n8n ni se comenzó Prompt 5.

**STAGE B — IMPLEMENTED / VALIDATED AGAINST LOCAL POSTGRESQL. WAITING FOR HUMAN REVIEW.**

## Ajuste previo al commit — 2026-09-09

Solicitud explícita del owner: confirmar/agregar Retry-After a los errores 503. No existía: los dos casos HTTP nuevos fallaron inicialmente con `KeyError: 'Retry-After'`. Se añadió `Retry-After: 30` en el handler central de 503, tanto para AUTHENTICATION_UNAVAILABLE como para IDENTITY_UNAVAILABLE. Valor fijo en segundos, coherente con el intervalo mínimo de refresh; no garantiza recuperación del proveedor ni de persistencia.

Se añadió una prueba parametrizada para ambos códigos y comprobaciones de que 200/401/403 no llevan Retry-After; 401 conserva WWW-Authenticate: Bearer. Contrato actualizado en PROMPT_4_SECURITY.md. Archivos afectados por este ajuste: main.py, test_identity_access.py y ambos documentos de Prompt 4.

Resultado de `python -m pytest backend/tests -q` después del cambio:

```text
201 passed, 12 skipped, 1 warning in 3.66s
```

Los 12 casos PostgreSQL son opt-in y no se volvieron a ejecutar para este ajuste HTTP; el punto 24 conserva la evidencia histórica de la validación anterior, no una nueva ejecución. Persiste la advertencia de deprecación de Starlette. No hubo cambios de arquitectura/esquema, conexiones externas, migraciones ni commit.

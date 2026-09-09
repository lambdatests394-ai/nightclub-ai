# Prompt 4 — Contrato de seguridad implementado

Fecha: 2026-09-08. Stage B autorizado por el checkpoint humano; entrega pendiente de revisión humana y sin commit de feature.

## Alcance y frontera de confianza

FastAPI verifica el Bearer JWT antes de consultar persistencia. Solo tras verificar firma y claims produce `CurrentUser(user_id: UUID)`, dataclass inmutable sin token, email ni rol organizacional. La identidad nunca procede de query, body, headers de usuario u organización ni claims sin verificar. Supabase Auth es el emisor previsto, no un cliente de negocio ni el motor de autorización organizacional.

Flujo: Bearer JWT -> verificador ES256/JWKS -> CurrentUser -> perfil activo -> repositorio acotado al usuario y organización -> OrganizationContext -> política RBAC -> respuesta explícita. No hay bypass administrativo ni acceso global para owner.

Solo se implementaron tres endpoints GET. No se implementaron login, registro, refresh, provisión de perfiles/membresías, mutaciones ni funcionalidades de otros módulos. FastAPI conserva ownership de autorización/persistencia; no se contactó Supabase remoto ni se implementaron Meta o n8n.

## JWT y configuración

Variables existentes reutilizadas, sin valores reales en el repositorio:

- `SUPABASE_JWT_ISSUER`: issuer HTTPS exacto; no prefijos ni derivación del token.
- `SUPABASE_JWT_AUDIENCE`: una audiencia esperada. PyJWT acepta `aud` string o array válido que la contenga.
- `SUPABASE_JWKS_URL`: URL HTTPS configurada por el operador, sin credenciales, query ni fragmento.

Variables añadidas:

- `SUPABASE_JWT_ALLOWED_ALGORITHMS=["ES256"]`: único conjunto permitido en Prompt 4.
- `JWT_CLOCK_SKEW_SECONDS=60`: leeway explícito para exp, iat y nbf. Validación de configuración: 0–300.
- `SUPABASE_JWKS_CACHE_TTL_SECONDS=300`: TTL local positivo, máximo 600 segundos.

Se requieren `iss`, `aud`, `exp`, `iat`, `sub`, `role`; `sub` debe ser UUID. Se verifica `nbf` cuando existe. NumericDate debe ser número finito, no bool/string; exp debe ser posterior a iat. `role` debe ser exactamente `authenticated`, nunca anon/service_role ni un rol de organización. `is_anonymous=true` se rechaza; si el claim existe debe ser booleano. Firma, algoritmo, issuer, audience y tiempo son obligatorios: no hay fallback HS256/RS256/none.

Un `kid` string de 1–128 caracteres selecciona únicamente claves confiables; el JWT no configura algoritmos o URLs. `jku`/`x5u` no se utilizan. Headers críticos no soportados y `b64` se rechazan. Tamaño de token limitado a 16 KiB. Si falta configuración de autenticación, las rutas protegidas no se habilitan por defecto: con Bearer devuelven 503; sin credencial, 401.

## Proveedor JWKS y caché

`JWKSProvider.fetch()` es inyectable y asíncrono. El adaptador de producción usa `httpx.AsyncClient` creado/cerrado en lifespan, TLS verificado, sin redirects ni proxy ambiental, timeout de 5 segundos por I/O y deadline total de 5 segundos. No descarga nada durante el arranque. Pruebas usan claves efímeras locales y transportes simulados; ninguna prueba contacta Supabase.

`JWKSCache` es por proceso, en memoria, sin archivo/tabla/Redis. Reloj monotónico, `asyncio.Lock`, publicación atómica del snapshot. Límites: 32 claves, 256 KiB de documento; solo claves públicas EC P-256 aptas para verificar ES256. Documentos corruptos, privados, duplicados o sin material compatible fallan cerrados.

Una clave conocida y caché no vencida se reutiliza. Ante kid desconocido o expiración hay como máximo un refresh por intento y un intervalo compartido mínimo de 30 segundos; las solicitudes concurrentes comparten el resultado del refresh. Un snapshot válido reciente que no contenga el kid produce 401, no otro refresh durante el intervalo. Si se necesita material confiable y falla el proveedor, devuelve 503, nunca autoriza. Un fallo no extiende el TTL anterior; claves anteriores aún válidas sí pueden verificar durante una caída. Un refresh exitoso reemplaza atómicamente el conjunto de claves, incluyendo rotación y retiro.

El TTL de 300 segundos controla **solo la caché local**. No garantiza visibilidad global de revocación en 300 segundos: caches del proveedor/intermediarios y solapamiento de claves también influyen. Rotación observada dentro del intervalo de 30 segundos puede denegarse temporalmente hasta el siguiente intento permitido. Cada worker conserva su propia caché; no hay coordinación distribuida ni revocación activa de sesiones.

## Membresía, contexto y permisos

El esquema real contiene los roles `owner`, `manager`, `editor`, `reviewer`, `operator`, `viewer`. No hay membership_id ni estado de membresía. Validez: perfil existente/activo, organización existente/activa y fila exacta `(organization_id, user_id)` con rol reconocido. Los repositorios vuelven a consultar en cada solicitud; no se cachean permisos o perfiles.

`OrganizationContext` es una dataclass inmutable con exactamente organization_id, user_id y role. Se construye después de validar la membresía; no se confía en el rol del token o del cliente. Las consultas filtran usuario verificado, organización cuando aplica, flags activos y roles reconocidos. Política pura síncrona, repositorio/red y servicios con E/S asíncronos.

| Rol de membresía válido | organization:read | Permiso no registrado |
| --- | --- | --- |
| owner | Permitido | Denegado |
| manager | Permitido | Denegado |
| editor | Permitido | Denegado |
| reviewer | Permitido | Denegado |
| operator | Permitido | Denegado |
| viewer | Permitido | Denegado |

Sin membresía exacta, o con rol desconocido, todo se deniega. No se definieron permisos de contenido, campañas, publicación, AI, Meta o automatización.

## Contratos HTTP

| Endpoint | Autorización | Datos |
| --- | --- | --- |
| GET /api/v1/me | CurrentUser + perfil activo | ProfileRead propio |
| GET /api/v1/me/organizations | CurrentUser + perfil activo | Solo organizaciones activas con membresías propias válidas, incluido role |
| GET /api/v1/organizations/{organization_id} | CurrentUser + perfil/organización activos + membresía exacta + organization:read | OrganizationRead |

En el tercer endpoint manda el path. Si existe `X-Organization-Id`, debe ser único, UUID válido y coincidir; si no, 403. Los endpoints personales no requieren selector. La convención de header sigue reservada para endpoints de negocio futuros.

Respuestas Pydantic camelCase `{data, meta}` con `meta.correlationId` y header `X-Correlation-Id` generado en servidor. Paginación: `limit` default 50, rango 1–100; `cursor` opaco Base64URL de UUID, `meta.nextCursor` o null, orden ascendente por ID. El cursor solo indica posición: no confiere autorización ni amplía los filtros. Respuestas usan campos explícitos, no serialización indiscriminada del ORM, y `Cache-Control: no-store`.

Errores esperados `application/problem+json`, código estable y correlationId sin stack/SQL/datos de entrada:

- 401 `AUTHENTICATION_FAILED`: Bearer ausente/malformado, JWT inválido o kid ausente en material confiable; incluye `WWW-Authenticate: Bearer`.
- 403 `ACCESS_DENIED`: identidad válida pero perfil ausente/inactivo, organización ausente/inactiva/ajena, membresía inválida, permiso denegado o selector inconsistente. La respuesta genérica no distingue existencia entre tenants.
- 503 `AUTHENTICATION_UNAVAILABLE`: configuración/material confiable requerido no disponible.
- 503 `IDENTITY_UNAVAILABLE`: persistencia no disponible/configurada.
- 422: parámetros/cursor malformados, sin eco de inputs. Un path que no sea UUID es error de contrato, no una consulta de existencia.

Ambas respuestas 503 incluyen `Retry-After: 30` (segundos): orientación fija de reintento alineada con el intervalo mínimo de refresh JWKS, no una garantía de recuperación. No se añade este header a 200/401/403.

Logging estructurado mínimo a stdout: correlationId, userId ya verificado, organizationId autorizado cuando aplica, plantilla de endpoint, status. No headers, JWT, queries, cuerpos, claves o mensajes crudos de excepciones. No introduce auditoría persistente ni observabilidad distribuida. Los access logs del servidor/proxy se deberán configurar para no registrar credenciales en URLs; los clientes deben enviar tokens únicamente por Authorization.

## Persistencia y RLS

No se creó migración 0003, tabla ni enum nuevo. Se alineó exclusivamente `OrganizationMember.role` con el enum PostgreSQL `member_role` existente (`create_type=False`) y `Profile.last_seen_at` con timestamptz. Pruebas comprueban el cast de parámetros a member_role, los seis valores reales y persistencia timezone-aware.

La FK profiles.id -> auth.users.id sigue gestionada por migración 0001; no hay entidad ORM auth.users. Autogenerate futuro debe considerar explícitamente esa FK externa: no debe eliminarla por no estar representada en metadata. `auth_stub.sql` no se amplió y solo satisface la FK en PostgreSQL local.

RLS tenant-aware **no está implementada ni validada en Prompt 4**. No se ejecutó `sql/003_rls.sql`. La frontera efectiva es FastAPI + JWT + contexto validado + RBAC + queries acotadas. ADR-001 conserva el objetivo de rol runtime mínimo y documenta la discrepancia/contrato futuro. El rol local `alembic_test_user` sirve exclusivamente para validación de migraciones, nunca como credencial runtime.

## Validación y límites operativos

Pruebas JWT/JWKS, políticas, HTTP, composición y SQLAlchemy sin servicios externos; integración opt-in sobre `nightclub_ai_prompt4_test` en loopback mediante el runbook. Claves privadas y JWT se generan en memoria durante las pruebas, nunca se guardan. Las pruebas async utilizan el plugin pytest de AnyIO con backend asyncio y esperan sus corutinas; las de Prompt 3 se conservan.

La verificación criptográfica presupone URL/issuer/audience configurados por un operador confiable, HTTPS válido y reloj del servidor correcto. No se valida un tenant Supabase real ni login/refresh/logout. No se garantiza invalidación inmediata del JWT por logout; la desactivación del perfil, organización o eliminación de membresía se consulta por solicitud. Una revocación concurrente después de la lectura no anula retroactivamente una respuesta ya autorizada; no hay mutaciones en este alcance.

`/health/ready` conserva el contrato de scaffold: no demuestra disponibilidad de DB/JWKS. Rate limiting, resiliencia distribuida, auditoría persistente, RLS y configuración de despliegue quedan para aprobación posterior. El TTL/cooldown limitan refrescos, no sustituyen protección de tráfico a nivel de despliegue.

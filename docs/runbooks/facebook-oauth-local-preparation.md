# Facebook OAuth: preparación local para revisión

Estado: implementación local, sin commit, sin despliegue y sin conexión real.
Fecha: 2026-10-04 UTC (2026-10-03, aprobación inicial en México).
Sincronización documental: 2026-10-05, con evidencia local aprobada; sin nueva
ejecución de pruebas ni infraestructura. Revisión humana de entrega pendiente;
certificación y habilitación de producción no realizadas.

## Comportamiento

La interfaz `/admin/facebook` utiliza correo y contraseña de la cuenta administrativa
de Night Club AI en Supabase, no la cuenta de n8n ni la contraseña de Facebook.
Envía el acceso directamente a Supabase Auth con una clave publishable.
Mantiene solamente el access token en memoria en la ventana principal; descarta
el refresh token y no usa cookies, localStorage ni sessionStorage.

La organización se introduce por UUID y la página por ID numérico. El inicio sigue
usando el endpoint autenticado existente y una nueva clave UUID de idempotencia.
La autorización abre una ventana auxiliar. El retorno público no intercambia
códigos, no consume estados y no consulta la base de datos.

La ventana principal acepta solamente un mensaje con origen exacto, ventana
auxiliar exacta y state igual al del inicio. Congela organización y sesión para
ese intento. Envía code/state por POST JSON a la API autenticada existente a
través de una nueva ruta de finalización, que reutiliza el coordinador.
El coordinador conserva JWT, organización, RBAC, state firmado, vínculo de
usuario/página/organización, vencimiento, consumo único y cifrado AES-256-GCM.

El callback GET existente conserva sus requisitos de Bearer y organización.
No se implementa publicación, creación de jobs ni cambios a n8n en esta interfaz.
La conexión guardada es activa según el contrato existente: antes de una futura
prueba real hay que revisar también los jobs pendientes del scheduler.

## Rutas

| Ruta | Función |
|---|---|
| `GET /admin/facebook` | Pantalla administrativa |
| `GET /admin/facebook/config` | Solo URL de Auth y clave publishable |
| `GET /admin/facebook/return` | Retorno de navegador sin permisos de persistencia |
| `GET /admin/facebook/assets/{name}` | Archivos estáticos permitidos explícitamente |
| `POST /api/v1/connections/facebook/oauth/start` | Inicio existente con JWT, organización e idempotencia |
| `POST /api/v1/connections/facebook/oauth/complete` | Finalización con JWT y organización |

La pantalla y la nueva ruta de finalización responden 404 mientras
`FACEBOOK_OAUTH_ADMIN_ENABLED=false`, que es el valor predeterminado.

## Configuración futura, todavía no aplicada

Además de las siete variables Meta identificadas previamente:

- `FACEBOOK_OAUTH_ADMIN_ENABLED`: mantener `false` hasta aprobar habilitación.
- `SUPABASE_URL`: origen HTTPS del proyecto de identidad.
- `SUPABASE_PUBLISHABLE_KEY`: exclusivamente `sb_publishable_...`. Se rechazan
  secret keys, service-role JWT y claves anon heredadas para esta pantalla.
- `SUPABASE_JWT_ISSUER`: debe coincidir exactamente con `SUPABASE_URL` + `/auth/v1`.
- Conservar audiencia, JWKS y verificación ES256 existentes.
- `META_OAUTH_REDIRECT_URI`: propuesta para el despliegue futuro:
  `https://nightclub-api-production.up.railway.app/admin/facebook/return`.
  No registrar todavía en Meta. La interfaz comprueba que corresponde al mismo
  origen donde se abrió la pantalla y a la ruta exacta de retorno.
- Versión Graph: confirmar con la app antes de configurar. `v24.0` se usa solo
  como dato sintético de prueba, no como recomendación de versión en producción.
- Claves de state y cifrado: independientes, 32 bytes base64url sin padding.
  No reutilizar el secreto HMAC de n8n. No generar ni sustituir claves existentes
  sin comprobar credenciales y recuperabilidad.

La pantalla no crea cuentas. La cuenta necesita membresía y rol owner/manager
en la organización. Soporta acceso mediante contraseña sin desafío MFA/CAPTCHA;
rechaza continuar si el resultado de Auth informa factores MFA verificados.
No debe habilitarse para un proyecto que exija CAPTCHA ni presentarse como un
flujo completo de MFA. No modificar esa política para hacer funcionar la demo.

## Registros y limpieza

No hay secretos interpolados en HTML. Las respuestas usan no-store,
no-referrer, CSP sin scripts inline, nosniff y bloqueo de frames.
El código de retorno elimina query y fragmento de la barra mediante replaceState
antes de cargar estilos y nunca reenvía error_description ni payload completo.
Esto NO borra la petición HTTP inicial de logs de proxy, plataforma o navegador.

Antes de una prueba real, conservar `--no-access-log` de Uvicorn y confirmar que
Railway, cualquier proxy y observabilidad no conservan queries del retorno,
headers Authorization ni cuerpos con code/token/password. Los logs permitidos
de la aplicación no incluyen esos valores. No afirmar saneamiento del proxy
basándose únicamente en pruebas de los logs de la aplicación.

Cancelar no llama a la finalización; el state no consumido vence naturalmente.
Cerrar el acceso limpia la memoria y la ventana pendiente; NO revoca una sesión
de Supabase ni tokens ya emitidos. Cerrar/reabrir/recargar exige autenticarse de nuevo.
JWT vencido, error de proveedor o state consumido requieren iniciar autorización
nueva; no hay reintento automático. El coordinador consume state antes de llamar
a Meta, incluso cuando falla el intercambio, como en el contrato anterior.

El popup depende de que el navegador y Facebook conserven opener. El comportamiento
real de Meta (incluidos COOP, bloqueadores y navegadores móviles) sigue pendiente
de comprobación en un ensayo autorizado. Una ventana sin opener no guarda nada.

## Pruebas locales

Requieren Python con `requirements.txt`; todas las identidades, claves, páginas y
respuestas de proveedor son sintéticas. No ejecutar flags opt-in de PostgreSQL
sin su checkpoint separado.

```sh
python -m pytest backend/tests/test_facebook_admin.py backend/tests/test_facebook_api.py backend/tests/test_facebook_oauth.py backend/tests/test_facebook_service.py backend/tests/test_facebook_oauth_provider.py -q
node --test backend/tests/facebook_admin_logic.test.cjs
python -m pytest backend/tests -q
```

Hay un ensayo Chromium preparado en `backend/tests/facebook_admin_browser.cjs`:
intercepta todas las peticiones y no utiliza proveedores reales. Requiere
Playwright `1.62.1` y su Chromium instalado en el entorno de pruebas, sin agregar
dependencias de navegador al runtime de FastAPI.

```sh
node backend/tests/facebook_admin_browser.cjs
```

Las pruebas de lógica JavaScript no sustituyen ese ensayo de navegador. La
instalación inicialmente bloqueada fue superada y el ensayo Chromium local se
ejecutó: cuatro escenarios independientes PASS (éxito, cancelación, sesión
expirada y logout), con cero proveedores reales contactados.

El retorno simulado utiliza `location.assign()` desde la página de consentimiento,
no `popup.goto()` para simular el retorno. No se asigna artificialmente
`window.opener`; se conservan las comprobaciones de origen, ventana y state.
Los cuerpos y cabeceras interceptados provienen de respuestas FastAPI/ASGI
locales con configuración sintética. Las tres lecturas de archivos estáticos
usan UTF-8 explícito, con regresión cp1252 documentada.
Este PASS local no demuestra el consentimiento auténtico ni COOP/opener de Meta.

## Evidencia y frontera de certificación

- Pruebas Python OAuth focalizadas: 74 PASS, exit 0.
- Lógica JavaScript, incluido rechazo de MFA: 8 PASS, exit 0.
- Chromium local: éxito, cancelación, sesión expirada y logout PASS por separado.
- PostgreSQL OAuth local: 25 PASS, una advertencia de caché pytest, exit 0;
  Alembic `20260928_0010`. Catálogos inicial/final idénticos, 58 políticas.
- Última suite general real: 1261 passed, 379 skipped, 4 failed, 1 warning,
  exit 1. No se repite durante esta sincronización documental ni se declara aprobada.
- Los cuatro rechazos históricos B7 son `CERTIFIED_SOURCE_DRIFT`, por cambios en
  `backend/app/core/config.py` y `backend/app/main.py` respecto a su baseline.
- Permanecen visibles: `test_current_repository_contract`,
  `test_direct_cli_is_offline_and_reports_scope`,
  `test_known_crlf_materialization_is_not_content_drift` y
  `test_hash_guarded_migration_still_requires_exact_bytes`.
- B7/B7.1, sus baselines, validadores y documentos históricos no se modifican
  para ocultar este cambio; no se agregan skip ni xfail. B7 histórico se acredita
  por separado: CLI PASS y 31 passed, exit 0, desde `1a1684af...` contra `301fe0f...`.
- Automatización actual: 253 passed, exit 0. Gate OAuth separado: PASS,
  16 fuentes OAuth y 46 entradas protegidas, con evidencia PostgreSQL aprobada.
- La revisión humana final de entrega permanece pendiente. El gate compuesto
  local propuesto no equivale a certificación ni autorización de producción.
- Las pruebas DB opt-in fueron ejecutadas y aprobadas únicamente en la instancia
  desechable del checkpoint separado. No se modificaron políticas/roles de
  producción ni SQL/migraciones; la sincronización actual no ejecuta DB ni pruebas.
- Los cambios B7.1 preexistentes en esta copia no forman parte del delta OAuth.
- Llamadas reales a Meta/Supabase Auth/n8n para este flujo: 0.
- Mutaciones de producción, publicaciones reales y activaciones: 0.

Permanece pendiente comprobar consentimiento auténtico de Meta, COOP/opener del
proveedor real, navegadores móviles/bloqueadores, MFA/CAPTCHA completo,
saneamiento de logs de proxy/plataforma y configuración/habilitación de producción.
OAuth permanece deshabilitado por defecto. La evidencia y sus catálogos están
referenciados en `docs/FACEBOOK_OAUTH_POSTGRESQL_RECERTIFICATION.md`; el historial
de bloqueos anteriores no representa el estado local vigente.

Referencias consultadas: [Supabase Auth password login](https://supabase.com/docs/reference/javascript/auth-signinwithpassword),
[API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[contrato Auth REST](https://github.com/supabase/auth/blob/master/openapi.yaml).
Se revisó el índice de changelog antes de integrar Auth. No se aplicaron cambios
a un proyecto Supabase, a su configuración ni a su esquema.

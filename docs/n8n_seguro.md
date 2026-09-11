# Puesta en marcha de los envíos seguros

Destino elegido: VPS Netcup de 2 vCore x86, 2 GB RAM y 60 GB SSD en Núremberg. Caddy será público; PostgreSQL y n8n permanecerán privados. Hoja de ruta vigente en `AGENTS.md`.

Estado: implementación local y pruebas automatizadas completadas. El usuario confirmó el 10/09/2026 las pruebas manuales del workflow con n8n local, PostgreSQL de Render y correo de prueba. No equivale a despliegue en el VPS ni a pruebas con datos reales. El export anterior se conserva sin modificar.

## Qué se entrega

- `workflows/n8n_envio_seguro.json`: importar como workflow nuevo, inactivo y sin credenciales.
- `workflows/n8n_delivery_ledger.sql`: tabla independiente para controlar duplicados, sin PDFs ni destinatarios.
- Flask exige HTTPS, una credencial explícita, certificado válido y respuesta JSON v1 asociada a la clave y al hash del PDF. No sigue redirecciones ni utiliza proxies/.netrc heredados del entorno.
- El registro de n8n solo guarda clave, huella del contenido, identificador de reserva, estado y fechas. No usar los datos estáticos de un workflow como deduplicación concurrente.
- Solo el propietario de una nueva reserva puede ejecutar SMTP. Una clave enviada devuelve el mismo acuse sin otro correo. Una reserva en curso o con resultado incierto devuelve 409; no caduca ni se reasigna automáticamente.
- El adjunto se recupera como binario real después de la consulta PostgreSQL. Se comprueban tamaño, MIME, extensión, firma básica, cierre, hash y campos permitidos. Esto no sustituye un análisis estructural/antimalware exhaustivo del PDF.
- `sent` significa que SMTP aceptó el destinatario y n8n persistió ese resultado. No garantiza recepción en bandeja de entrada ni lectura.

## Pasos en n8n

1. Importa `workflows/n8n_envio_seguro.json` como un workflow distinto. No sobrescribas ni actives todavía el anterior. No debe haber dos rutas de envío utilizables en producción.
2. Genera un secreto aleatorio en tu equipo con `openssl rand -hex 32`. No lo pegues en chats, JSON, repositorio ni capturas.
3. Crea una credencial **Header Auth**: Name `X-Workflow-Token`, Value el secreto. Selecciónala en el nodo **Webhook**, que ya exige `Header Auth`. Sin esa credencial no se puede poner en servicio el flujo.
4. Usa la base de la app (`gestion_abonos`, nombre propuesto) con el esquema separado `n8n_delivery` para el registro de entregas. El motor n8n usará otra base lógica (`n8n`) dentro de esa misma instancia PostgreSQL. Su usuario interno es distinto de la credencial PostgreSQL de los nodos del workflow.
5. **En las pruebas actuales con Render**, no hay que crear un PostgreSQL local ni Docker. Usa la URL privada de la base de pruebas y ejecuta primero `workflows/n8n_delivery_ledger.sql`. Comprueba si tu usuario tiene permiso para crear roles (`CREATEROLE`). Si Render no lo permite, deja el ledger creado y pospone el usuario `n8n_mailer` hasta el VPS; no intentes solucionarlo publicando la base.

6. **En el VPS**, cuando PostgreSQL ya esté instalado, crea primero las bases `gestion_abonos` y `n8n`. Después, desde SSH o una red administrativa, ejecuta el siguiente comando contra `gestion_abonos`:

   ```sh
   psql -X -h localhost -U postgres -d gestion_abonos -f workflows/setup_n8n_permissions.sql
   ```

   Hazlo desde el servidor/red administrativa, sin publicar PostgreSQL. El comando crea la tabla y el usuario `n8n_mailer`, concede `CONNECT`, `USAGE` y `SELECT, INSERT, UPDATE`, y pide dos veces una contraseña nueva (usa una aleatoria guardada en tu gestor). No escribas la clave en el comando. No crea otra base ni el usuario interno del motor n8n.

   El alta es transaccional: si hay un error, permisos amplios detectados de `PUBLIC` o el usuario ya existe, se cancela sin cambiar usuarios existentes. No es un script de actualización ni de rotación de contraseñas. Comprueba tablas/columnas, secuencias, creación de esquemas/objetos y funciones `SECURITY DEFINER` de la base seleccionada; no sustituye la revisión de permisos futuros, otras bases o extensiones. PostgreSQL concede normalmente `CONNECT` y `TEMPORARY` a `PUBLIC`: limitar conexiones de este usuario a la base de la app mediante `pg_hba.conf`, y revisar esos permisos al preparar el VPS. El script no revoca permisos globales que puedan afectar a la app.

   Configura una credencial PostgreSQL con `n8n_mailer` y esa contraseña en **Reservar envio** y **Registrar enviado**. Activa TLS con verificación de certificado si hay conexión por red; no actives «Ignore SSL Issues». La contraseña y el certificado se configuran en credenciales, no en las consultas. Referencias: [contraseñas con psql](https://www.postgresql.org/docs/current/app-psql.html), [privilegios y PUBLIC](https://www.postgresql.org/docs/current/ddl-priv.html).
8. En **Enviar correo**, selecciona tu credencial SMTP y comprueba el remitente autorizado. Usa TLS: preferiblemente puerto 465 con SSL/TLS; si usas 587, verifica STARTTLS obligatorio según el proveedor. No habilites certificados no válidos. Adjunto: propiedad `pdf`. No actives «Retry on Fail», «Continue on Fail», CC ni BCC. Comprueba que el PDF llega como adjunto al importar en tu versión de n8n.
9. El nodo **Validar solicitud** utiliza el módulo incorporado `crypto`. En n8n Cloud está disponible según su documentación. Si alojas n8n tú mismo, permite únicamente `crypto` mediante `NODE_FUNCTION_ALLOW_BUILTIN=crypto` en el entorno del ejecutor Code/task runner correspondiente. No habilites `*` ni módulos externos innecesarios. El límite del workflow es 5 MB por PDF; si cambias el límite de Flask, coordina ambos.
10. En ajustes del workflow confirma que NO se guardan datos de ejecuciones exitosas, fallidas, manuales ni progreso. El JSON ya solicita estos valores. Evita datos fijados («pinned data») y pruebas con PDFs reales en el editor. Restringe cuentas del editor a gestores autorizados, activa MFA y excluye este workflow de MCP.
11. Con credenciales y restricciones de red configuradas, publica/activa el workflow nuevo. Copia su **Production URL** del nodo Webhook. Revisa que termina en `/webhook/recibir-pdf-seguro-v1`, nunca `/webhook-test/…`.
12. Desactiva el antiguo workflow sin autenticación. Retira sus datos de pruebas, ejecuciones antiguas y binarios almacenados según la política de conservación; desactivar el guardado ahora no elimina copias ya existentes ni backups del proveedor.

Referencias: [Webhook y autenticación](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/), [consultas parametrizadas de PostgreSQL](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.postgres/), [ajustes del workflow](https://docs.n8n.io/build/manage-workflows/configure-workflow-settings), [módulos del nodo Code](https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/configuration-examples/enable-modules-in-code-node).

## Configuración de Flask

En `.env` local o secretos del proveedor; no versionar los valores:

```dotenv
N8N_WEBHOOK_URL=https://TU_HOST/webhook/recibir-pdf-seguro-v1
N8N_WEBHOOK_SECRET_HEADER=X-Workflow-Token
N8N_WEBHOOK_SECRET=REEMPLAZAR_POR_EL_SECRETO_ALEATORIO_GENERADO
N8N_WEBHOOK_BEARER_TOKEN=
N8N_WEBHOOK_TIMEOUT_SECONDS=20
N8N_WEBHOOK_MAX_RETRIES=2
```

Usa exactamente el mismo secreto de la credencial Header Auth. El marcador del ejemplo no es una credencial válida para producción. Reinicia Flask después de configurar las variables. Solo se admite un método: la alternativa Bearer requiere cambiar la credencial del Webhook a una Header Auth con Name `Authorization` y Value `Bearer <secreto>`, dejar `N8N_WEBHOOK_SECRET` vacío y configurar `N8N_WEBHOOK_BEARER_TOKEN` con el secreto sin el prefijo.

Los dos reintentos automáticos solo se aplican a `ConnectTimeout`. Timeout de lectura, errores TLS, respuestas HTTP fallidas o acuses incorrectos quedan sin confirmar y no se repiten dentro de esa petición. Pulsar enviar de nuevo reutiliza la misma clave; esto solo es seguro con el workflow nuevo y su registro persistente. Nunca borres ese registro para «arreglar» un error.

## Que el webhook no sea accesible desde fuera

HTTPS y Header Auth protegen el transporte y la autorización, pero **no convierten una URL pública en privada**. El aislamiento depende del despliegue, aún no configurado:

- Arquitectura preparada en `compose.yaml`: contenedores separados para Flask, PostgreSQL, n8n y runners. Caddy publica la aplicación, ofrece el editor solo en `127.0.0.1:8443` y escucha el webhook interno en el puerto no publicado `9443`. Flask valida explícitamente la CA interna mediante `N8N_CA_CERT_FILE`; no acepta HTTP ni `verify=False`. PostgreSQL y n8n no publican sus puertos. Guía operativa en `DEPLOYMENT.md`. Falta instalarla y validarla en el VPS real.
- Alternativa descartada para esta primera etapa, n8n Cloud: el webhook tiene una URL pública. Configura la lista blanca de IP del Webhook con una salida fija y exclusiva de Flask cuando el proveedor lo permita. Verifica el filtrado real desde una red no autorizada y no confíes en cabeceras reenviadas que el cliente pueda falsificar. Si necesitas ausencia total de exposición pública, el VPS con red privada encaja mejor.
- PostgreSQL: sin puerto público salvo acceso estrictamente limitado por firewall cuando sea necesario. Credenciales separadas para app, base interna de n8n y registro de correos.
- Limitar peticiones y tamaño del cuerpo en el proxy de entrada (por ejemplo, 8 MB por petición para un único PDF de hasta 5 MB). No exponer rutas de test, API ni editor en el proxy del webhook.
- No incluir cuerpos, cabeceras de autenticación o PDFs en logs del proxy/observabilidad. Revisar retención de binarios y datos de ejecución también en fallos y backups. Los administradores del servidor y del proveedor siguen formando parte del perímetro de confianza.

## Recuperación y pruebas antes de uso real

La prueba inicial debe ser con PDF sintético y buzón propio autorizado. No probar fallos con entradas de clientes.

- Sin credencial / credencial incorrecta: rechazo y cero correos.
- PDF ausente, hash alterado, tipo desconocido o email con varios destinatarios: HTTP 400, cero correos.
- Petición válida de entrada y parking: un adjunto exacto por petición y acuse v1; comprobar hash del archivo recibido.
- Repetir y lanzar simultáneamente la misma clave: como máximo una ejecución SMTP. Si ya figura `sent`, devuelve acuse; si está `processing`, devuelve 409.
- Cortar conexión tras SMTP: Flask no marca enviado sin acuse. Repetir solo consulta/reutiliza la clave. Si quedó `processing`, consultar el proveedor SMTP antes de intervenir: puede haberse enviado.
- Si SMTP acredita aceptación, el administrador puede reconciliar el registro a `sent` con evidencia. Si acredita que NO se envió, definir una intervención auditada para permitir nuevo intento. Sin evidencia, mantener el resultado incierto. No eliminar reservas ni usar «retry from failed node» para saltarse la reserva.
- Un fallo después de reservar y antes de SMTP también queda bloqueado: se prioriza evitar duplicados sobre reenvíos automáticos sin confirmación. No hay garantía de exactamente una entrega ni reconciliación automática con SMTP.
- Cambio/liberación de asignación o destinatario durante el lote: se revalida cada entrega y se bloquean sus filas mientras se realiza HTTP. El resto del lote no mantiene esos bloqueos.
- Liberar conserva registros enviados y fallidos; solo cancela pendientes. Reasignar al mismo destinatario no repite un PDF enviado. Una reserva cancelada sin intentos puede reactivarse conservando su identidad.
- Reiniciar Flask/n8n/PostgreSQL y repetir pruebas. Restaurar los registros de deduplicación junto a los backups: perderlos puede permitir duplicados.

Pruebas locales ejecutables sin correo real:

```sh
.venv/bin/python -m unittest discover -s tests -p test_email_delivery_security.py -v
node tests/test_n8n_workflow.cjs
TEST_DATABASE_URL=postgresql+psycopg2://BD_DESECHABLE .venv/bin/python -m unittest discover -s tests -v
```

Los tests JS ejecutan la lógica de los nodos Code, pero no sustituyen la importación y ejecución real de n8n. La prueba PostgreSQL usa un esquema aislado; nunca apuntar `TEST_DATABASE_URL` a producción.

## Alojamiento elegido

Netcup, Núremberg: 2 vCore x86, 2 GB RAM, 60 GB SSD y pago mensual. Precio orientativo aportado por el usuario: unos 3,13 €/mes con IVA español; no se ha verificado la contratación. Añadir dominio, backups externos y, si procede, servicio de correo/IPv4.

Una instancia PostgreSQL basta: `gestion_abonos` contiene las tablas de la app y el esquema `n8n_delivery`; otra base lógica `n8n` contiene los datos internos del motor. Tres identidades independientes: app, usuario limitado de los nodos del workflow y usuario interno n8n. No hacen falta tres servidores ni otra instancia PostgreSQL.

## Orden de creación de PostgreSQL

1. Ahora, en Render, mantener la PostgreSQL gratuita solo para pruebas. Crear el esquema `n8n_delivery`, cargar datos sintéticos y validar el workflow. No asumir que ese plan permite crear roles propios.
2. Al contratar el VPS, levantar PostgreSQL con Compose o instalarlo en el host, sin publicar su puerto. Crear `gestion_abonos` para Flask y `n8n` para el motor n8n.
3. Crear roles separados para administración, Flask, workflow y motor n8n. Ejecutar `setup_n8n_permissions.sql` sobre `gestion_abonos` y configurar en n8n la credencial de tipo **Postgres** para `n8n_mailer`.
4. Antes de usar datos reales, restaurar una copia de prueba, comprobar permisos con cada rol y probar backup y restauración. Render no migra automáticamente la base al VPS.

Los 2 GB se validarán con carga realista, incluidos runners, lotes PDF y backups. Arrancar con un worker y sin infraestructura adicional innecesaria; medir RAM, disco y tiempos antes de abrir uso real. Si falta margen, ampliar RAM. Los snapshots del proveedor no sustituyen un backup cifrado fuera del VPS con restauración probada.

Compose, Caddy y el TLS interno se validaron localmente con contenedores y datos desechables. La prueba no acredita firewall, DNS, certificados públicos, carga, backups ni aislamiento en el VPS real.

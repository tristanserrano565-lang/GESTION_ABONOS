# Vincular la aplicación con Cloudflare Access por 0 €

Esta guía configura Cloudflare DNS y Access delante del dominio público sin usar Cloudflare Tunnel, WARP ni servicios de pago. Está pensada para los 2–3 gestores de esta aplicación.

Cloudflare Access Free cuesta 0 USD para equipos de hasta 50 usuarios. Cloudflare puede pedir un método de pago durante el alta de Zero Trust incluso al escoger el plan gratuito; comprueba que el resumen indique **Free / 0** antes de confirmar. El VPS y el registro del dominio se pagan por separado.

## Resultado final

El acceso seguirá este recorrido:

```text
Gestor → Cloudflare Access → Caddy → validación JWT en Flask → login de la aplicación
```

Cada gestor tendrá que superar dos barreras:

1. Introducir su email autorizado y el código temporal enviado por Cloudflare.
2. Iniciar sesión con su cuenta propia de la aplicación.

## Datos que debes tener preparados

- El dominio raíz añadido a Cloudflare, por ejemplo `tudominio.es`.
- El subdominio de la aplicación, por ejemplo `abonos.tudominio.es`.
- La IPv4 pública del VPS y, si vas a usarla, su IPv6.
- La lista exacta de emails de los gestores.
- Acceso al registrador donde compraste el dominio.

No guardes contraseñas ni tokens de Cloudflare en el repositorio. El **team domain** y el **AUD** que se usarán más adelante no son secretos.

## 1. Añadir el dominio al plan Free

1. Entra en el [panel de Cloudflare](https://dash.cloudflare.com/).
2. Selecciona **Onboard a domain** o **Add a domain**.
3. Escribe el dominio raíz, por ejemplo `tudominio.es`, sin `https://` ni subdominios.
4. Selecciona el plan **Free**. No selecciones Pro, Business ni complementos.
5. Cloudflare mostrará dos nameservers. Entra en el registrador del dominio y sustituye allí los nameservers actuales por esos dos.
6. Espera hasta que Cloudflare muestre la zona como **Active**.

El plan Free admite la configuración DNS completa necesaria para Zero Trust. Referencia: [añadir un sitio para Access](https://developers.cloudflare.com/learning-paths/clientless-access/initial-setup/add-site/).

## 2. Crear el registro DNS

En **Websites → tu dominio → DNS → Records**:

1. Crea un registro `A`:
   - **Name:** `abonos`.
   - **IPv4 address:** IPv4 pública del VPS.
   - **Proxy status:** inicialmente **DNS only** (nube gris).
   - **TTL:** Auto.
2. Crea un registro `AAAA` solamente si has configurado y comprobado IPv6 en el VPS. Déjalo también inicialmente en **DNS only**.
3. Arranca Caddy siguiendo `docs/DEPLOYMENT.md` y comprueba que `https://abonos.tudominio.es` presenta un certificado público válido.
4. Vuelve al registro y cambia **Proxy status** a **Proxied** (nube naranja).

No cargues datos reales mientras el registro esté en nube gris, porque en esa fase se puede llegar directamente al origen.

## 3. Seleccionar Full (strict)

En **Websites → tu dominio → SSL/TLS → Overview** selecciona **Full (strict)**. Este modo cifra los dos tramos y comprueba el certificado presentado por Caddy. No uses `Flexible`.

Referencia: [SSL/TLS Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).

## 4. Crear Zero Trust Free

1. Desde el panel principal abre **Zero Trust**.
2. Crea la organización si todavía no existe.
3. Elige un **team name** corto y reconocible, por ejemplo `gestion-abonos-equipo`. Cloudflare creará el dominio `gestion-abonos-equipo.cloudflareaccess.com`.
4. Selecciona expresamente **Zero Trust Free** y comprueba que el total sea `0` antes de aceptar.
5. Activa 2FA en la cuenta administradora de Cloudflare y guarda los códigos de recuperación fuera del VPS.

Puedes consultar el team domain posteriormente en **Zero Trust → Settings → Team name and domain**. Cloudflare documenta que el plan Free cubre este uso y que puede solicitar datos de pago durante el alta sin cobrar al escoger Free: [crear una organización Zero Trust](https://developers.cloudflare.com/learning-paths/clientless-access/initial-setup/create-zero-trust-org/).

## 5. Activar One-time PIN

Las organizaciones nuevas pueden traer el proveedor de identidad de Cloudflare como opción predeterminada. Para que los gestores no necesiten una cuenta de Cloudflare, añade OTP:

1. Ve a **Zero Trust → Integrations → Identity providers**.
2. En **Your identity providers**, pulsa **Add new identity provider**.
3. Selecciona **One-time PIN** y guarda.
4. Usa únicamente **One-time PIN** como método de inicio de sesión de la aplicación que crearás después.

Cloudflare enviará un código temporal al email que intente entrar. La política del paso siguiente decide qué direcciones pueden recibir acceso. Referencia: [configurar One-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/).

## 6. Crear la aplicación protegida

Ve a **Zero Trust → Access controls → Applications**:

1. Pulsa **Create new application**.
2. Selecciona **Self-hosted and private**.
3. Selecciona **Add public hostname**. No elijas una ruta privada ni crees un Tunnel.
4. Configura:
   - **Application name:** `Gestion de abonos`.
   - **Session duration:** `8 hours` o menos.
   - **Domain:** selecciona `tudominio.es`.
   - **Subdomain:** `abonos`.
   - **Path:** déjalo vacío para proteger toda la aplicación.
5. En los métodos de identidad permitidos deja **One-time PIN**. Si aparece **Instant authentication**, puedes activarlo al existir un solo método.
6. Guarda o continúa hasta **Access policies**.

La aplicación debe cubrir exactamente `abonos.tudominio.es`, sin comodines. Las aplicaciones Access deniegan por defecto si ninguna política `Allow` coincide. Referencia: [publicar una aplicación self-hosted](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).

## 7. Permitir únicamente a los gestores

Crea una política dentro de la aplicación:

- **Policy name:** `Allow gestores`.
- **Action:** `Allow`.
- **Include:** selector **Emails**.
- Añade una regla con la dirección completa de cada gestor.

Ejemplo:

```text
gestor1@empresa.es
gestor2@empresa.es
administrador@empresa.es
```

No uses `Everyone`, dominios completos, `Emails ending in`, rangos de IP ni reglas generales de bypass. Una persona que conozca la URL pero no use uno de esos emails debe quedar rechazada por Cloudflare.

La aplicación publica `/robots.txt` y `X-Robots-Tag` para desaconsejar la indexación cuando Access esté desactivado durante una prueba. No crees una excepción Access para esa ruta: en producción debe permanecer protegida como el resto de la aplicación. Estas señales solo las respetan rastreadores cooperativos; Access y el firewall son los controles que bloquean peticiones no autorizadas.

## 8. Crear la excepción mínima para renovar el certificado

El Caddy actual renueva el certificado mediante ACME. Para que Access no interrumpa únicamente ese reto, crea otra aplicación más específica:

1. Vuelve a **Access controls → Applications → Create new application**.
2. Selecciona **Self-hosted and private → Add public hostname**.
3. Usa el mismo dominio y subdominio.
4. En **Path** escribe `.well-known/acme-challenge/*`.
5. Nómbrala `ACME Caddy`.
6. Crea una política:
   - **Policy name:** `Bypass ACME`.
   - **Action:** `Bypass`.
   - **Include:** `Everyone`.
7. No añadas más rutas ni políticas a esta aplicación.

La ruta más específica tiene prioridad sobre la aplicación general. Esta excepción solo permite llegar al manejador del reto de Caddy; Flask continúa rechazando peticiones sin JWT cuando no existe un reto activo. Referencia: [prioridad de rutas de Access](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/).

## 9. Copiar el team domain y el AUD

Necesitas dos valores para que Flask pueda verificar que el JWT pertenece a tu organización y a esta aplicación:

1. Copia el **team domain** desde **Zero Trust → Settings → Team name and domain**. Debe tener este formato:

   ```text
   gestion-abonos-equipo.cloudflareaccess.com
   ```

2. Ve a **Access controls → Applications → Gestion de abonos → Configure**.
3. Abre **Additional settings** y copia **Application Audience (AUD) Tag**.
4. En el VPS edita `/opt/gestion-abonos/.env.deploy`:

   ```dotenv
   CLOUDFLARE_ACCESS_ENABLED=true
   CLOUDFLARE_ACCESS_TEAM_DOMAIN=gestion-abonos-equipo.cloudflareaccess.com
   CLOUDFLARE_ACCESS_AUD=pega-aqui-el-audience-tag
   ```

5. No pongas `https://`, rutas ni barra final en `CLOUDFLARE_ACCESS_TEAM_DOMAIN`.
6. Aplica la configuración:

   ```sh
   cd /opt/gestion-abonos
   chmod 600 .env.deploy
   docker compose --env-file .env.deploy config --quiet
   docker compose --env-file .env.deploy up -d --build
   docker compose --env-file .env.deploy ps
   ```

Cloudflare asigna un AUD diferente a cada aplicación. Flask valida el JWT recibido en `Cf-Access-Jwt-Assertion` contra ese valor y contra las claves públicas de tu team domain. Referencia: [obtener y validar el AUD](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/).

## 10. Cerrar el acceso directo al VPS

Cuando el DNS ya esté en naranja y Access funcione:

1. En el firewall de Netcup permite TCP 80 y 443 únicamente desde los [rangos IP actuales de Cloudflare](https://www.cloudflare.com/ips/).
2. Aplica la misma allowlist en UFW siguiendo `docs/DEPLOYMENT.md`.
3. Mantén SSH en una regla independiente desde tu IP administrativa.
4. No publiques los puertos 5432, 5678, 8000, 8443 ni 9443.

Esto evita que alguien use directamente la IP del VPS para saltarse Access. Caddy también está preparado para aceptar `CF-Connecting-IP` únicamente cuando la conexión procede de uno de esos rangos.

## 11. Comprobaciones obligatorias

Hazlas antes de introducir PDFs o datos auténticos:

1. Abre el dominio en una ventana privada. Debe aparecer primero Cloudflare Access.
2. Usa un email permitido. Debes recibir el OTP y llegar después al login propio de Flask.
3. Usa una segunda ventana privada con un email ajeno. Cloudflare debe rechazarlo.
4. Después de superar Access, confirma que el login de Flask sigue siendo obligatorio.
5. Cambia temporalmente un carácter del AUD en `.env.deploy`, recrea `web` y comprueba que Flask devuelve `403 Acceso denegado.`. Restaura inmediatamente el AUD correcto y recrea `web`.
6. Visita una ruta inexistente dentro de `/.well-known/acme-challenge/`. Debe devolver `403` o `404`, pero no la pantalla de autenticación de Access.
7. Comprueba desde otra conexión que `https://IP_DEL_VPS` y `http://IP_DEL_VPS` no responden.
8. Confirma en los registros que el rate limiting identifica la IP pública del gestor y no una IP de Cloudflare.
9. Reinicia los contenedores y repite una entrada correcta:

   ```sh
   docker compose --env-file .env.deploy restart
   docker compose --env-file .env.deploy ps
   ```

Si Cloudflare permite el acceso pero Flask muestra `Acceso denegado.`, revisa primero el team domain, el AUD y que `CLOUDFLARE_ACCESS_ENABLED=true`. Si todos los usuarios aparecen con la misma IP, revisa los rangos de `deploy/Caddyfile`, el firewall y que Cloudflare no esté eliminando `CF-Connecting-IP`.

## Qué debes evitar para mantener el coste en 0 €

- No cambies Cloudflare Zero Trust Free a Pay-as-you-go.
- No contrates Pro o Business para la zona DNS.
- No actives complementos con precio mostrado, Browser Isolation de pago ni soporte contratado.
- No crees Cloudflare Tunnels, Workers o reglas adicionales para esta aplicación: no son necesarios para el diseño actual.
- Revisa siempre el importe antes de confirmar cualquier cambio de plan.

Con 2–3 gestores, DNS Free, Zero Trust Free y OTP son suficientes. Cloudflare seguirá costando 0 € mientras mantengas esos planes y no contrates productos adicionales. La página oficial presenta Access Free a 0 USD para equipos de menos de 50 usuarios: [precios de Cloudflare Access](https://www.cloudflare.com/sase/products/access/).

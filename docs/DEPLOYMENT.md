# Despliegue en producción con Docker Compose

Esta guía parte del estado actual: el stack funciona en local, pero todavía no existe el VPS. El destino es un VPS Netcup x86 con 2 vCore, 2 GB de RAM y 60 GB SSD. Cloudflare Access protegerá el dominio proxied y Caddy será el único servicio expuesto en el VPS; Flask, PostgreSQL, n8n y sus task runners se comunicarán por una red Docker privada. No se usará Cloudflare Tunnel ni será necesario instalar WARP en los equipos gestores.

La arquitectura final será:

```text
Internet
   |
   v
Cloudflare DNS proxied + Access
   |
   | HTTPS; solo gestores autorizados
   v
Caddy
   |
   +-- Flask + Gunicorn :8000 (solo Docker)
   |
   +-- n8n webhook :9443 HTTPS (solo Docker)
           |
           +-- PostgreSQL :5432 (solo Docker)
           +-- Gmail SMTP :587 STARTTLS (salida)

Administrador --SSH tunnel--> n8n editor :8443 (solo 127.0.0.1)
```

## Reglas que no debes romper

- No copies al VPS `.env`, `.env.deploy`, `deploy/secrets/`, PDFs de prueba ni dumps sin cifrar.
- Genera secretos nuevos en el VPS. Los secretos locales son únicamente para pruebas.
- No publiques los puertos `5432`, `5678`, `8000`, `8443` ni `9443`.
- No confíes solo en la pantalla de Access: cierra el acceso directo a la IP del VPS y conserva el login de Flask.
- No ejecutes `docker compose down -v`: `-v` elimina los volúmenes persistentes.
- No borres `postgres_data` para aplicar cambios. Los scripts de inicialización solo se ejecutan al crear el volumen por primera vez.
- No uses datos ni correos reales hasta superar la comprobación final con datos sintéticos.
- Ejecuta Docker, firewall y administración del VPS fuera del `venv`. El `venv` solo se usa para desarrollo y pruebas Python locales.

## Servicios y persistencia

| Servicio | Función | Exposición |
| --- | --- | --- |
| Cloudflare Access | Primera autenticación por identidad | Solo el dominio proxied; no entra en Docker |
| `caddy` | HTTPS de origen, proxy y CA interna | TCP 80/443 solo desde Cloudflare; 8443 solo loopback |
| `web` | Flask con Gunicorn | Solo red Docker, puerto 8000 |
| `postgres` | Bases `gestion_abonos` y `n8n` | Solo red Docker, puerto 5432 |
| `n8n` | Motor, editor y webhook | Solo red Docker, puerto 5678 |
| `n8n-runners` | Nodos Code aislados | Solo red Docker |
| `caddy-ca` | Copia la CA pública interna para Flask | Sin red pública |

Los volúmenes `postgres_data`, `n8n_data`, `caddy_data`, `caddy_config` y `caddy_ca` sobreviven a la recreación de contenedores. La base `gestion_abonos` contiene las tablas de la app y `n8n_delivery.receipts`; la base lógica `n8n` contiene la configuración interna de n8n. El usuario `n8n_mailer` solo accede al registro de entregas.

## Fase 0. Dejar listo el proyecto local

### 0.1. Terminar las pruebas locales

Antes de comprar el VPS, confirma:

- La aplicación abre en `https://localhost`.
- El editor abre en `https://localhost:8443`.
- El workflow está importado y publicado; la URL de producción termina en `/webhook/recibir-pdf-seguro-v1`, nunca en `/webhook-test/...`.
- Un envío sintético entrega el PDF correcto.
- Repetir la misma clave no manda un segundo correo.
- Un error queda visible en la aplicación y no se marca como enviado.

Consulta logs locales con:

```sh
docker compose --env-file .env.deploy ps
docker compose --env-file .env.deploy logs --tail=100 web n8n n8n-runners caddy postgres
docker compose --env-file .env.deploy logs -f web n8n
```

### 0.2. Preparar el repositorio

El repositorio puede ser público si esa publicación es intencionada y se revisa antes de cada commit. El código, la arquitectura y el workflow serán visibles; la seguridad no debe depender de ocultarlos. `docs/` y `workflows/` se versionan porque contienen la guía y el workflow revisado sin credenciales. `.env.deploy`, `deploy/secrets/`, exports JSON sueltos, PDFs, dumps y la CA local permanecen ignorados.

Antes de hacer commit:

```sh
git status --short
git diff --check
git check-ignore .env .env.deploy deploy/secrets/n8n_webhook_secret caddy-local-root.crt
```

Los cuatro elementos del último comando deben aparecer como ignorados. Revisa manualmente todo lo que vayas a subir y busca posibles secretos:

```sh
git diff --cached
git grep --cached -n -i -E 'password|secret|token|api[_-]?key' -- ':!docs/DEPLOYMENT.md'
```

No uses `git add .` sin revisar. Añade únicamente los archivos previstos, haz commit y súbelos a la rama de producción. Comprueba en GitHub que `deploy/secrets/`, `.env*`, PDFs y dumps no aparecen.

### 0.3. Decidir el origen de los datos

La recomendación actual es empezar producción con una base vacía porque Render contiene datos de prueba. Flask creará las tablas y el administrador inicial al arrancar.

Si más adelante decides conservar datos de Render, detén este procedimiento antes de importarlos. Hay que preparar un `pg_dump` del esquema `public`, revisar los PDFs globales antiguos y ensayar la restauración. No ejecutes un `pg_restore --clean` genérico: podría afectar el ledger y sus permisos.

### 0.4. Cerrar la confianza entre Cloudflare y el origen

La aplicación ya incluye las dos protecciones del origen. Prepara la aplicación de Access en la fase 1.3 para obtener su audiencia y configúralas antes de activar el proxy en la fase 7.1:

- Con `CLOUDFLARE_ACCESS_ENABLED=true`, Flask valida `Cf-Access-Jwt-Assertion`: solo RS256, firma contra las claves de `https://NOMBRE-EQUIPO.cloudflareaccess.com/cdn-cgi/access/certs`, emisor y audiencia exactos, token de tipo `app`, identidad con email y límites temporales. Si falta o falla, devuelve `403` antes de autenticación o lógica de negocio. La descarga de claves usa HTTPS verificado, sin redirects ni proxies del entorno, está limitada a 64 KB y se cachea una hora. El `aud` no es un secreto.
- Caddy confía `CF-Connecting-IP` únicamente cuando el peer pertenece a los CIDR oficiales de Cloudflare incluidos en `deploy/Caddyfile`. Sobrescribe `X-Forwarded-For` con un solo valor y Flask conserva `PROXY_FIX_X_FOR=1`. Una conexión directa no puede imponer su propia IP mediante cabeceras.

`/healthz` queda fuera de la validación JWT para el healthcheck interno y no accede a datos. Cloudflare Access seguirá protegiéndolo en el borde cuando se visita por el dominio. Antes de desplegar, compara los CIDR de Caddy y del firewall con las listas oficiales actuales; si Cloudflare cambia sus redes, actualiza ambos antes de reiniciar Caddy.

## Fase 1. Comprar VPS y dominio

### 1.1. VPS

Al contratar Netcup:

1. Elige el VPS x86 acordado en Núremberg.
2. Confirma que incluye una IPv4 pública; guarda también la IPv6 si está disponible.
3. Instala una imagen mínima de **Debian estable x86_64**. No uses una imagen con panel de hosting, Docker o n8n preinstalado.
4. Guarda en tu gestor de contraseñas los accesos de CCP/SCP y activa 2FA si Netcup lo ofrece para tu cuenta.
5. Anota la IPv4, IPv6, identificador del servidor y acceso inicial. No los guardes en el repositorio.
6. No confíes en snapshots como único backup: siguen dentro del mismo proveedor y cuenta.

### 1.2. Dominio

Necesitas un nombre público, por ejemplo `abonos.tudominio.es`, y la zona DNS debe estar activa en Cloudflare. Añade el dominio a Cloudflare y sustituye en el registrador sus nameservers por los indicados en el panel. Activa 2FA en la cuenta administradora de Cloudflare y guarda sus códigos de recuperación fuera del VPS.

En Cloudflare DNS crea:

- Registro `A`: `abonos.tudominio.es` → IPv4 del VPS.
- Registro `AAAA`: solo si vas a configurar y comprobar IPv6 → IPv6 del VPS.

Empieza temporalmente como **DNS only** (nube gris) para que Caddy obtenga su primer certificado. No compartas el dominio ni cargues datos reales mientras esté así. Espera a que resuelva:

```sh
dig +short A abonos.tudominio.es
dig +short AAAA abonos.tudominio.es
```

El registro `A` debe devolver la IP del VPS. Si publicas `AAAA`, comprueba también el acceso por IPv6; si no funciona todavía, elimina temporalmente `AAAA`. Caddy obtiene el primer certificado público cuando el dominio resuelve al servidor y TCP 80/443 están accesibles, según los [requisitos de Automatic HTTPS](https://caddyserver.com/docs/automatic-https). La nube naranja y el cierre del origen se activan después del primer arranque.

### 1.3. Preparar Cloudflare Access

Cloudflare Access funciona en el navegador y no requiere Cloudflare Tunnel ni instalar WARP. Antes de activar el proxy:

La configuración completa del panel, incluidos el plan gratuito, OTP, la política, el AUD y las comprobaciones finales, está en `docs/CLOUDFLARE_ACCESS.md`.

1. Crea la organización Zero Trust y elige el plan **Free**. Cloudflare indica que durante el alta puede pedir datos de pago incluso en este plan, pero no cobra por seleccionarlo; revisa el resumen antes de confirmar.
2. En **Zero Trust → Integrations → Identity providers**, habilita **One-time PIN**. Para 2–3 gestores es la opción más sencilla: Cloudflare envía un código de un solo uso al email permitido.
3. En **Zero Trust → Access controls → Applications**, crea una aplicación **Self-hosted and private → Add public hostname**.
4. Usa exactamente `abonos.tudominio.es`, sin comodines ni rutas adicionales. Nómbrala `Gestion de abonos` y fija **Session Duration** en `8 hours` o menos.
5. Crea una política `Allow gestores`. En **Include**, añade cada dirección completa con el selector **Emails**. No uses `Everyone`, `Emails ending in`, un dominio entero ni solo `Login Methods`: cualquiera de esas opciones ampliaría el acceso más de lo previsto.
6. En **Require**, selecciona **Login methods → One-time PIN**. Si el panel ofrece MFA independiente, actívalo para esta aplicación; OTP por email por sí solo depende de la seguridad del buzón.
7. En los ajustes de cookies, conserva `HttpOnly` y activa **Binding Cookie** si aparece disponible. No actives **Authenticate with Cloudflare One Client**, porque aquí no se usará WARP y es incompatible con Binding Cookie.
8. Comprueba con **Test policies** cada email permitido y uno ajeno. Access es `deny by default`: quien no coincida con una regla `Allow` no debe entrar.
9. Como Caddy usa ACME HTTP para renovar el certificado, crea una segunda aplicación Access limitada exactamente a `abonos.tudominio.es/.well-known/acme-challenge/*`. Dentro de esa aplicación crea una política `Bypass ACME` con acción `Bypass` e `Include → Everyone`. Esta es la única excepción pública admisible: queda limitada al reto de Caddy y no da acceso a rutas de Flask. Tras activar Access, `curl -I https://abonos.tudominio.es/.well-known/acme-challenge/comprobacion` debe llegar al origen y devolver `403` o `404` cuando no exista un reto activo, sin redirigir al login de Access. Si prefieres no tener esta excepción, hay que sustituir antes el certificado automático actual por Cloudflare Origin CA o DNS-01; esa adaptación no está implementada en el stack actual.

No crees reglas `Bypass` generales. El login, las sesiones, los roles y el rate limiting de Flask se mantienen: Access es una primera barrera y no sustituye las cuentas de la aplicación. Cuando un gestor deje de tener acceso, elimínalo de la política de Access y desactiva o elimina también su usuario de Flask.

Cerrar sesión en Flask no cierra automáticamente la sesión de Access. Esto es aceptable porque volver a la aplicación sigue exigiendo las credenciales de Flask, pero para retirar a una persona de inmediato debes quitar su email de la política, revocar su usuario/sesiones en Cloudflare y desactivar su cuenta de Flask.

Referencia oficial: [alta de Zero Trust](https://developers.cloudflare.com/cloudflare-one/setup/), [aplicaciones self-hosted de Access](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), [OTP por email](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/) y [políticas de Access](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/).

## Fase 2. Primer acceso y seguridad básica

Sustituye `IP_VPS` por la IP real. Conserva abierta la primera sesión hasta verificar una segunda conexión. Si aún no tienes una clave SSH local, créala; no sobrescribas una existente:

```sh
test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
ssh-copy-id root@IP_VPS
```

### 2.1. Entrar y actualizar Debian

Desde tu ordenador:

```sh
ssh root@IP_VPS
```

En el VPS:

```sh
apt update
apt full-upgrade -y
apt install -y sudo ufw ca-certificates curl dnsutils git nano unattended-upgrades netcat-openbsd
timedatectl set-timezone Europe/Madrid
hostnamectl set-hostname gestion-abonos
systemctl enable --now unattended-upgrades
```

Si se actualizó el kernel, reinicia y vuelve a entrar:

```sh
reboot
```

### 2.2. Crear el usuario de administración

Entra otra vez como root y crea un usuario; en esta guía se llama `deploy`:

```sh
adduser deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

Desde **otra terminal local**, verifica antes de continuar:

```sh
ssh deploy@IP_VPS
sudo -v
```

Solo cuando esa conexión funcione, crea `/etc/ssh/sshd_config.d/99-hardening.conf` en el VPS:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Valida y recarga SSH sin cerrar la sesión actual:

```sh
sudo sshd -t
sudo systemctl reload ssh
```

Abre una tercera sesión `ssh deploy@IP_VPS`. Si falla, corrige SSH desde la sesión que mantuviste abierta o desde la consola SCP.

### 2.3. Crear swap para el VPS de 2 GB

Comprueba primero si ya existe:

```sh
swapon --show
free -h
```

Si no existe, crea 2 GB una sola vez:

```sh
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-gestion-abonos.conf
sudo sysctl --system
```

Verifica de nuevo con `swapon --show`. La swap evita una caída brusca ante un pico, pero no sustituye ampliar RAM si las pruebas reales consumen demasiado.

## Fase 3. Firewall de Netcup y UFW

### 3.1. Firewall de Netcup

En SCP abre **Firewall**. Para una instalación nueva, Netcup indica que su firewall viene activo y que la política `netcup Mail block` bloquea SMTP. Durante el primer arranque, mientras el DNS esté en nube gris y no haya datos reales, crea o asigna reglas temporales de entrada para:

- TCP 22 desde tu IP si es fija; desde cualquier origen si cambia frecuentemente.
- TCP 80 desde cualquier origen.
- TCP 443 desde cualquier origen.

No autorices entradas a `5432`, `5678`, `587`, `8000`, `8443` ni `9443`.

Después de que Caddy obtenga el certificado y actives el proxy de Cloudflare, sustituye las reglas públicas de 80/443 por reglas que acepten **solo** los rangos IPv4 e IPv6 publicados por Cloudflare. Hazlo primero en el firewall de Netcup; es la barrera que no depende de las reglas que Docker añade en el VPS. Consulta siempre la lista vigente en [Cloudflare IP Ranges](https://www.cloudflare.com/ips/) y revísala periódicamente, porque una incorporación futura debe añadirse antes de bloquear el resto. El puerto SSH conserva su regla independiente y nunca se limita a las IP de Cloudflare.

Para enviar mediante Gmail tendrás que retirar `netcup Mail block`; Netcup documenta que bloquea SMTP entrante y saliente. La aplicación solo necesita salida TCP 587. Quitar el bloqueo del proveedor no abre un servicio SMTP en el VPS: UFW seguirá negando entradas y Compose no publica ese puerto. Consulta la [documentación oficial del firewall de Netcup](https://www.netcup.com/en/helpcenter/documentation/server/firewall).

Si configuras reglas de salida propias en SCP, recuerda que Netcup cambia la política implícita de salida a `DROP`: entonces también debes permitir DNS, NTP, HTTP/HTTPS y TCP 587. No actives una política de salida restrictiva hasta saber qué resolutores y servicios usa el VPS.

### 3.2. UFW en Debian

En el VPS, antes de habilitarlo, confirma que SSH usa el puerto 22. Si lo cambiaste, sustituye la primera regla:

```sh
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'SSH'
sudo ufw allow 80/tcp comment 'Caddy HTTP temporal'
sudo ufw allow 443/tcp comment 'Caddy HTTPS temporal'
sudo ufw enable
sudo ufw status verbose
```

Comprueba que `/etc/default/ufw` contiene `IPV6=yes`. Mantén abierta la sesión SSH actual y prueba otra conexión antes de seguir.

Docker puede gestionar reglas de red por debajo de UFW. Por eso la defensa principal consiste también en no publicar puertos privados en `compose.yaml`. En este proyecto solo se publican TCP 80/443 y `127.0.0.1:8443`.

Cuando el registro pase a proxied, sustituye también en UFW las dos reglas temporales por una regla de 80 y otra de 443 para cada CIDR oficial de Cloudflare. No copies una lista antigua desde esta guía. Confirma antes que la misma allowlist ya está aplicada en Netcup; si te equivocas, usa la consola SCP para recuperarlo. La [documentación de Cloudflare](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/) recomienda bloquear en el origen cualquier otro acceso web para impedir que la IP pública eluda sus controles.

Puedes descargar la lista vigente para revisarla, sin ejecutar directamente contenido remoto como root:

```sh
curl -fsS https://www.cloudflare.com/ips-v4 -o /tmp/cloudflare-ips-v4.txt
curl -fsS https://www.cloudflare.com/ips-v6 -o /tmp/cloudflare-ips-v6.txt
cat /tmp/cloudflare-ips-v4.txt
cat /tmp/cloudflare-ips-v6.txt
```

Tras comprobar los CIDR, elimina las reglas genéricas con `sudo ufw delete allow 80/tcp` y `sudo ufw delete allow 443/tcp`, añade para cada línea reglas de esta forma y revisa el resultado antes de cerrar la consola SCP:

```sh
sudo ufw allow from CIDR_CLOUDFLARE to any port 80 proto tcp
sudo ufw allow from CIDR_CLOUDFLARE to any port 443 proto tcp
sudo ufw status numbered
```

## Fase 4. Instalar Docker Engine y Compose

Instala Docker desde su repositorio oficial. No uses el script rápido `get.docker.com` en producción. Sigue los comandos vigentes de [Docker Engine para Debian](https://docs.docker.com/engine/install/debian/):

```sh
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Crea `/etc/apt/sources.list.d/docker.sources` con:

```text
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
```

`trixie` corresponde a Debian 13. Si Netcup instala otra versión, usa el valor de `VERSION_CODENAME` mostrado por `cat /etc/os-release`.

Instala y comprueba:

```sh
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

No añadas `deploy` al grupo `docker`: ese grupo equivale prácticamente a acceso root. En esta guía todos los comandos Docker llevan `sudo`. El complemento `docker compose` es el método soportado; el binario antiguo `docker-compose` no es necesario.

## Fase 5. Descargar el código de forma segura

### 5.1. Dar al VPS acceso de solo lectura al repositorio

Como `deploy`, genera una clave exclusiva para este repositorio:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/gestion_abonos_deploy -N '' -C 'deploy gestion-abonos'
cat ~/.ssh/gestion_abonos_deploy.pub
```

Añade la clave pública en GitHub como **Deploy key** de solo lectura. No marques permiso de escritura. Después crea `~/.ssh/config`:

```text
Host github-gestion-abonos
    HostName github.com
    User git
    IdentityFile ~/.ssh/gestion_abonos_deploy
    IdentitiesOnly yes
```

Protege y prueba:

```sh
chmod 600 ~/.ssh/config ~/.ssh/gestion_abonos_deploy
ssh -T github-gestion-abonos
```

GitHub responderá que la autenticación ha funcionado aunque no ofrezca shell.

### 5.2. Clonar

Sustituye `PROPIETARIO/REPOSITORIO.git`:

```sh
sudo install -d -o deploy -g deploy /opt/gestion-abonos
git clone git@github-gestion-abonos:PROPIETARIO/REPOSITORIO.git /opt/gestion-abonos
cd /opt/gestion-abonos
git status --short
```

La salida final debe estar vacía. Comprueba que existen:

```sh
test -f compose.yaml
test -f Dockerfile
test -f deploy/Caddyfile
test -f deploy/postgres/init-databases.sh
test -f workflows/n8n_envio_seguro.json
```

## Fase 6. Crear la configuración de producción

### 6.1. Variables no secretas

En `/opt/gestion-abonos`:

```sh
cp .env.deploy.example .env.deploy
nano .env.deploy
```

Ejemplo:

```dotenv
APP_DOMAIN=abonos.tudominio.es
ACME_EMAIL=tu-correo-administrativo@example.com
DEFAULT_ADMIN_USERNAME=admin
CLOUDFLARE_ACCESS_ENABLED=true
CLOUDFLARE_ACCESS_TEAM_DOMAIN=nombre-equipo.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=audiencia-copiada-de-la-aplicacion-access
```

No añadas protocolo, ruta ni barra final a `APP_DOMAIN` ni a `CLOUDFLARE_ACCESS_TEAM_DOMAIN`. El dominio de equipo debe terminar exactamente en `.cloudflareaccess.com`. Copia `CLOUDFLARE_ACCESS_AUD` desde **Application Audience (AUD) Tag** en la configuración de la aplicación Access. `.env.deploy` no contiene contraseñas, pero tampoco se versiona porque pertenece a ese servidor. Protégelo con `chmod 600 .env.deploy`.

El `.env` de la raíz se conserva únicamente para ejecutar Flask directamente en desarrollo y no se copia a la imagen Docker. No lo adaptes ni lo subas al VPS. En producción, Compose ya aplica los valores de seguridad:

| Ajuste | Producción | Motivo |
| --- | --- | --- |
| `COOKIE_SECURE` | `true` en `compose.yaml` | La cookie de sesión solo viaja por HTTPS. |
| `FORCE_HTTPS` | `false` en `compose.yaml` | Caddy ya redirige HTTP a HTTPS; mantenerlo así permite el healthcheck HTTP privado del contenedor. |
| `ENABLE_SECURITY_HEADERS` | `true` en `compose.yaml` | Activa CSP, HSTS y otras cabeceras. |
| `SESSION_COOKIE_SAMESITE` | `Lax` en `compose.yaml` | Mantiene protección razonable sin romper la navegación normal. |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `1800` en `compose.yaml` | Revoca en servidor una sesión tras 30 minutos sin peticiones. |
| `SESSION_MAX_AGE_SECONDS` | `28800` en `compose.yaml` | Exige autenticarse de nuevo después de 8 horas aunque exista actividad. |
| `PROXY_FIX_*` | fijados en `compose.yaml` | Flask acepta únicamente la capa de proxy prevista dentro de Docker. |
| `CLOUDFLARE_ACCESS_ENABLED` | `true` en `.env.deploy` de producción | Hace obligatorio un JWT de Access válido en todas las rutas salvo `/healthz`. En local se omite o se deja en `false`. |
| `CLOUDFLARE_ACCESS_TEAM_DOMAIN` | dominio de equipo sin protocolo | Fija el emisor y la URL HTTPS de claves públicas. |
| `CLOUDFLARE_ACCESS_AUD` | Audience Tag de la aplicación | Impide aceptar tokens emitidos para otra aplicación del mismo equipo. |
| credenciales y claves | `deploy/secrets/` | Se generan de nuevo en el VPS y nunca se escriben en `.env.deploy`. |

Actualiza `.env.deploy` en producción con esos seis valores. Los límites de caché, timeout y edad máxima del token ya están fijados en `compose.yaml`; no los cambies sin una necesidad medida.

### 6.2. Secretos nuevos

No copies `deploy/secrets` desde local. Genera todo en el VPS:

```sh
python3 deploy/generate-secrets.py
```

El script:

- pide la contraseña inicial del administrador de Flask;
- pide opcionalmente la API key de API-Football;
- genera claves distintas para PostgreSQL, Flask, n8n, webhook y runners;
- no sobrescribe secretos existentes.

Usa una contraseña de administrador única de 16 caracteres o más y guárdala en tu gestor. Verifica sin mostrar valores:

```sh
test "$(find deploy/secrets -maxdepth 1 -type f | wc -l)" -eq 11
stat -c '%a %n' deploy/secrets
```

El directorio debe mostrar modo `700`. No imprimas los archivos salvo cuando necesites copiar un valor concreto a una credencial de n8n desde una sesión administrativa.

### 6.3. Validar la configuración antes de crear datos

```sh
sudo docker compose --env-file .env.deploy config --quiet
sudo docker compose --env-file .env.deploy pull
sudo docker compose --env-file .env.deploy build --pull
```

Si `config --quiet` falla, no arranques. Corrige primero la ruta o variable indicada.

## Fase 7. Primer arranque de producción

Arranca una única vez con el volumen PostgreSQL vacío:

```sh
sudo docker compose --env-file .env.deploy up -d
sudo docker compose --env-file .env.deploy ps
```

Espera hasta que `postgres`, `web` y `n8n` aparezcan como `healthy`. Revisa logs:

```sh
sudo docker compose --env-file .env.deploy logs --tail=150 postgres web n8n n8n-runners caddy caddy-ca
```

La primera inicialización debe crear:

- roles `postgres_admin`, `gestion_app`, `n8n_owner` y `n8n_mailer`;
- bases `gestion_abonos` y `n8n`;
- esquema `n8n_delivery` y tabla `receipts`;
- tablas de Flask y usuario administrador inicial;
- CA interna de Caddy y certificado público del dominio.

Comprueba el certificado público:

```sh
curl -I https://abonos.tudominio.es
sudo docker compose --env-file .env.deploy logs --tail=100 caddy
```

Si Caddy no consigue el certificado, revisa DNS, registros `AAAA`, firewall de Netcup, UFW y que ningún otro proceso ocupe 80/443:

```sh
sudo ss -lntp
dig +short A abonos.tudominio.es
dig +short AAAA abonos.tudominio.es
```

### 7.1. Activar Cloudflare sin dejar una vía directa

Solo después de que Caddy tenga un certificado válido:

1. En **Cloudflare → SSL/TLS → Overview**, selecciona **Full (strict)**. No uses `Flexible` ni desactives la verificación del certificado de origen.
2. En **DNS**, cambia el registro `A` y, si existe, `AAAA` a **Proxied** (nube naranja). Al repetir `dig`, deben aparecer direcciones de Cloudflare, no la IP del VPS.
3. Confirma que la aplicación Access y su política `Allow gestores` están activas. En una ventana privada, el dominio debe mostrar primero Cloudflare Access y solo después el login de Flask.
4. Aplica en el firewall de Netcup la allowlist vigente de Cloudflare para TCP 80/443 y elimina las reglas temporales abiertas a todo Internet. Repite la restricción en el firewall efectivo del VPS. Conserva SSH aparte.
5. Desde una red externa, verifica que no existe bypass directo:

```sh
curl -kI --connect-timeout 5 --resolve abonos.tudominio.es:443:IP_VPS https://abonos.tudominio.es/
```

La conexión directa debe fallar o ser rechazada. Ejecuta además el mismo intento por IPv6 si publicaste `AAAA`. Si devuelve la aplicación, no uses datos reales: el firewall sigue abierto o no está actuando antes de las reglas de Docker.

6. Prueba tres casos en el navegador: email permitido, email no permitido y sesión de Access caducada. Después entra también con la cuenta correcta de Flask; superar Access por sí solo no debe autenticar en la aplicación.
7. Comprueba la excepción ACME: solo `/.well-known/acme-challenge/*` puede evitar Access y debe terminar en Caddy. Cualquier otra ruta, incluidas `/`, `/login`, `/static/` y endpoints POST, debe exigir Access.

La nube naranja oculta la IP en las consultas DNS nuevas, pero no borra históricos ni evita el acceso directo por sí sola. La protección efectiva es la combinación de Access, validación JWT en Flask, proxy DNS y cierre del origen. Authenticated Origin Pulls en Caddy puede añadirse como barrera adicional, pero no sustituye la validación de identidad de Access.

Revisa tras el cambio que el rate limiting sigue distinguiendo clientes. Caddy contiene una copia fechada de los CIDR oficiales y Flask confía únicamente en Caddy; no amplíes `PROXY_FIX_*`. Si todos los intentos aparecen con una misma IP de Cloudflare, detén el despliegue y revisa `CF-Connecting-IP`, la lista de Caddy y que Cloudflare no tenga activado **Remove visitor IP headers**.

Referencias: [Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/), [protección de la IP de origen](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/), [validación del JWT de Access](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) y [Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/).

## Fase 8. Configurar n8n de producción

La instancia del VPS es nueva: no hereda usuarios, credenciales ni workflows del n8n local.

### 8.1. Abrir el editor mediante túnel SSH

En tu ordenador, deja esta terminal abierta:

```sh
ssh -N -L 8443:127.0.0.1:8443 deploy@IP_VPS
```

La primera vez debes confiar en la CA interna creada **en el VPS**, que es distinta de la CA local. En otra terminal del VPS:

```sh
cd /opt/gestion-abonos
sudo docker compose --env-file .env.deploy cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-vps-root.crt
sudo chown deploy:deploy ./caddy-vps-root.crt
```

Desde tu ordenador:

```sh
scp deploy@IP_VPS:/opt/gestion-abonos/caddy-vps-root.crt ./caddy-vps-root.crt
sudo trust anchor --store ./caddy-vps-root.crt
```

Reinicia completamente el navegador y abre `https://localhost:8443`. En Firefox puede ser necesario importarlo en **Ajustes → Privacidad y seguridad → Certificados → Autoridades** y marcar confianza para sitios web.

Después de copiarlo, elimina la copia adicional del directorio del proyecto en el VPS; Caddy conserva el original en su volumen:

```sh
rm /opt/gestion-abonos/caddy-vps-root.crt
```

Crea el propietario de n8n con una contraseña única y activa MFA. El editor seguirá inaccesible desde Internet porque el puerto 8443 está vinculado a `127.0.0.1`.

### 8.2. Importar y configurar el workflow

Importa `workflows/n8n_envio_seguro.json`. Configura estas credenciales:

**Postgres**, en `Reservar envio` y `Registrar enviado`:

```text
Host: postgres
Port: 5432
Database: gestion_abonos
User: n8n_mailer
Password: contenido de deploy/secrets/n8n_mailer_password
SSL: desactivado, porque permanece en la red Docker del mismo host
```

**Header Auth**, en el nodo `Webhook`:

```text
Name: X-Workflow-Token
Value: contenido de deploy/secrets/n8n_webhook_secret
Allowed HTTP Request Domains: None
```

`Allowed HTTP Request Domains: None` impide reutilizar esa credencial en nodos HTTP Request/GraphQL; no bloquea las peticiones entrantes al webhook.

**SMTP Gmail**, en `Enviar correo`:

```text
User: cuenta Gmail completa
Password: contraseña de aplicación de Google, no la contraseña normal
Host: smtp.gmail.com
Port: 587
SSL/TLS: desactivado
Disable STARTTLS: desactivado
Client Host Name: vacío
```

Esta combinación usa STARTTLS. Si usas el puerto 465, entonces activa `SSL/TLS`; no mezcles 587 con TLS implícito. Google exige autenticación y recomienda TLS; consulta sus [ajustes SMTP](https://support.google.com/mail/answer/7104828). El export público lleva `remitente@example.com` como marcador: sustituye `From Email` en el nodo por la cuenta autenticada o por un alias autorizado antes de publicar.

Comprueba desde el VPS que Netcup permite la conexión:

```sh
nc -vz smtp.gmail.com 587
```

Guarda el workflow y pulsa **Publish**. Copia visualmente su Production URL y confirma que la ruta exacta es:

```text
/webhook/recibir-pdf-seguro-v1
```

No uses la Test URL y no pulses `Execute workflow` para dejarlo escuchando: un workflow publicado registra permanentemente la Production URL.

## Fase 9. Pruebas de aceptación

### 9.1. Estado y exposición

En el VPS:

```sh
cd /opt/gestion-abonos
sudo docker compose --env-file .env.deploy ps
sudo docker compose --env-file .env.deploy logs --tail=100 web n8n caddy postgres
sudo ss -lntp
sudo ufw status verbose
```

Desde otro equipo o red, comprueba los puertos:

```sh
nmap -Pn -p 22,80,443,5432,5678,8000,8443,9443 IP_VPS
```

Resultado esperado:

- `22`: accesible según la regla SSH.
- `80` y `443` de la IP del VPS: accesibles únicamente desde los rangos de Cloudflare; desde el equipo externo usado para `nmap` deberían aparecer cerrados o filtrados.
- `5432`, `5678`, `8000`, `8443` y `9443`: cerrados o filtrados.
- `https://abonos.tudominio.es`: certificado público válido de Cloudflare y pantalla de Access antes de Flask.
- `https://IP_VPS:8443`: inaccesible desde Internet.

### 9.2. Aplicación

Con datos sintéticos:

1. Entra con el administrador inicial y cambia la contraseña si la compartiste temporalmente.
2. Crea un operador de prueba y confirma que no puede administrar usuarios.
3. Inicia esa cuenta en un segundo navegador y confirma que la primera sesión deja de funcionar.
4. Cambia su contraseña y confirma que vuelve al login y que ninguna sesión anterior funciona.
5. Crea clientes de prueba, incluidos dos con el mismo email.
6. Crea abonos y parkings.
7. Crea o sincroniza un partido en casa.
8. Carga varios PDFs sintéticos y revisa asociación automática/manual.
9. Asigna recursos y comprueba el resumen de listos.
10. Envía a una cuenta tuya de prueba y verifica PDF, partido, destinatario y remitente.
11. Repite la acción y confirma que no llega un duplicado.
12. Prueba liberar/reasignar y confirma que el historial previo permanece.
13. Provoca un fallo controlado de SMTP y verifica que la aplicación no marca `sent`.

Sigue los logs durante la prueba:

```sh
sudo docker compose --env-file .env.deploy logs -f web n8n
```

No deben aparecer destinatarios completos, contenido de PDFs, tokens ni contraseñas.

### 9.3. Reinicio completo

```sh
sudo reboot
```

Tras volver a conectar:

```sh
cd /opt/gestion-abonos
sudo docker compose --env-file .env.deploy ps
curl -I https://abonos.tudominio.es
```

Confirma que usuarios, workflow, credenciales, datos y ledger siguen presentes y que el workflow continúa publicado.

## Fase 10. Backup antes de abrir producción

Crea un directorio solo para root:

```sh
sudo install -d -m 700 /var/backups/gestion-abonos
```

Haz dumps de ambas bases desde `/opt/gestion-abonos`:

```sh
sudo sh -c 'docker compose --env-file .env.deploy exec -T postgres pg_dump -U postgres_admin -d gestion_abonos -Fc > /var/backups/gestion-abonos/gestion_abonos.dump'
sudo sh -c 'docker compose --env-file .env.deploy exec -T postgres pg_dump -U postgres_admin -d n8n -Fc > /var/backups/gestion-abonos/n8n.dump'
sudo ls -lh /var/backups/gestion-abonos
```

También debes guardar cifrados fuera del VPS:

- ambos dumps;
- `deploy/secrets/`, especialmente `n8n_encryption_key`;
- la versión/commit de código desplegada;
- instrucciones de restauración.

No copies secretos ni dumps a almacenamiento externo sin cifrarlos. Un snapshot de Netcup sirve para una reversión rápida, pero no sustituye un backup cifrado en otra cuenta/proveedor. Antes de usar datos reales, ensaya la restauración en un stack desechable y verifica que n8n puede descifrar sus credenciales con la clave restaurada.

La automatización y rotación de backups queda como tarea separada porque requiere elegir el destino externo y sus credenciales.

## Fase 11. Abrir el uso controlado

Abre el sistema a los 2–3 gestores únicamente cuando todo lo siguiente esté marcado:

- [ ] Dominio y HTTPS público válidos.
- [ ] DNS `A/AAAA` proxied, SSL/TLS `Full (strict)` y Cloudflare Access activo.
- [ ] Política Access limitada a emails completos; un email ajeno queda rechazado.
- [ ] Login de Flask sigue siendo obligatorio después de Access.
- [ ] JWT `Cf-Access-Jwt-Assertion` validado criptográficamente en el origen.
- [ ] Acceso directo a la IP del VPS rechazado en IPv4 y, si existe, IPv6.
- [ ] Renovación ACME comprobada mediante la excepción limitada a `/.well-known/acme-challenge/*` o certificado de origen alternativo documentado.
- [ ] SSH solo con clave; root y contraseña desactivados.
- [ ] Firewall Netcup y UFW revisados para IPv4/IPv6.
- [ ] Solo SSH es accesible directamente; 80/443 aceptan únicamente Cloudflare.
- [ ] Caddy/Flask restauran la IP real sin confiar cabeceras de orígenes distintos de Cloudflare.
- [ ] PostgreSQL, Flask, n8n y runner saludables.
- [ ] Editor n8n accesible únicamente mediante túnel SSH.
- [ ] Propietario n8n con MFA.
- [ ] Workflow publicado en Production URL y Header Auth correcto.
- [ ] Credencial PostgreSQL `n8n_mailer` con permisos mínimos.
- [ ] Gmail por TCP 587 y STARTTLS con contraseña de aplicación.
- [ ] Prueba de envío, deduplicación, error y reinicio superada.
- [ ] Prueba con lote real dentro de los límites de 5 MB por PDF y 25 MB por lote.
- [ ] RAM, swap y disco observados durante la carga.
- [ ] Backup cifrado externo y restauración ensayada.
- [ ] Procedimiento para revisar envíos inciertos conocido por los gestores.

## Actualizaciones posteriores

No actualices directamente sin backup. Procedimiento normal:

```sh
cd /opt/gestion-abonos
git fetch origin
git status --short
git log --oneline HEAD..origin/main
sudo docker compose --env-file .env.deploy config --quiet
```

Si la revisión es correcta:

```sh
git pull --ff-only
sudo docker compose --env-file .env.deploy pull
sudo docker compose --env-file .env.deploy up -d --build
sudo docker compose --env-file .env.deploy ps
sudo docker compose --env-file .env.deploy logs --tail=100 web n8n postgres caddy
```

Las bases y secretos persisten. Si la actualización cambia el esquema, ejecuta únicamente la migración documentada y ensayada para esa versión. `create_all` crea tablas faltantes, pero no transforma tablas existentes.

Para volver al código anterior, identifica el commit previo y reconstruye; no reviertas una migración de base de datos sin su procedimiento específico:

```sh
git log --oneline -5
git checkout COMMIT_ANTERIOR
sudo docker compose --env-file .env.deploy up -d --build
```

Después de estabilizar, vuelve a la rama principal. No dejes producción permanentemente en detached HEAD.

## Comandos diarios útiles

```sh
# Estado
sudo docker compose --env-file .env.deploy ps

# Logs recientes
sudo docker compose --env-file .env.deploy logs --tail=100

# Logs del envío en directo
sudo docker compose --env-file .env.deploy logs -f web n8n

# Uso de recursos
sudo docker stats
df -h
free -h

# Reiniciar un servicio sin borrar datos
sudo docker compose --env-file .env.deploy restart web

# Parar y arrancar conservando volúmenes
sudo docker compose --env-file .env.deploy down
sudo docker compose --env-file .env.deploy up -d
```

Evita `docker system prune --volumes` y cualquier comando con `down -v` en producción.

## Incidencias frecuentes

### El dominio no obtiene certificado

Revisa `A/AAAA`, TCP 80/443, hora del sistema y logs de Caddy. Un `AAAA` incorrecto puede hacer fallar la validación IPv6.

### Access no aparece y se ve directamente Flask

Comprueba que el registro esté en **Proxied**, que la aplicación self-hosted use exactamente el hostname y que tenga asociada la política. No abras el uso hasta que una ventana privada muestre Access antes del login de Flask.

### Access funciona por dominio, pero la IP permite saltárselo

Las reglas del origen no están cerradas. Limita TCP 80/443 a los CIDR oficiales actuales de Cloudflare en el firewall de Netcup y en el firewall efectivo del host; revisa también IPv6. Docker puede eludir reglas simples de UFW, por lo que la comprobación externa y el firewall del proveedor son obligatorios.

### Caddy no puede renovar el certificado tras activar Access

Comprueba que la aplicación específica `/.well-known/acme-challenge/*` tiene acción `Bypass` y que esa URL llega a Caddy sin mostrar Access. La excepción no debe abarcar ninguna otra ruta. Como solución más cerrada, migra el origen a Cloudflare Origin CA o DNS-01 antes de retirar la excepción.

### Flask recibe HTTP 404 de n8n

El workflow no está publicado o la ruta no coincide. Usa Production URL `/webhook/recibir-pdf-seguro-v1`; no uses `/webhook-test`.

### Flask recibe 401/403 de n8n

La credencial Header Auth no está asociada al Webhook o su valor no coincide con `deploy/secrets/n8n_webhook_secret`.

### Gmail muestra `wrong version number`

Se mezcló TLS implícito con el puerto 587. Usa `587`, `SSL/TLS` desactivado y `Disable STARTTLS` desactivado; o `465` con `SSL/TLS` activado.

### Gmail exige `Application-specific password`

Activa 2FA en Google y usa una contraseña de aplicación. La contraseña normal de la cuenta no sirve.

### Gmail da timeout desde Netcup

Ejecuta `nc -vz smtp.gmail.com 587` y revisa que se haya retirado `netcup Mail block` en SCP. No abras 587 como puerto entrante.

### Un contenedor no está healthy

```sh
sudo docker compose --env-file .env.deploy ps
sudo docker compose --env-file .env.deploy logs --tail=200 NOMBRE_SERVICIO
```

No recrees volúmenes como solución automática; primero identifica la causa.

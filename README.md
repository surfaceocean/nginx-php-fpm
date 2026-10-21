# Nginx + PHP-FPM

Nginx and PHP-FPM in one container, maintained by [surfaceocean](https://github.com/surfaceocean/nginx-php-fpm), based on [jkaninda/nginx-php-fpm](https://github.com/jkaninda/nginx-php-fpm).

[![Build](https://github.com/surfaceocean/nginx-php-fpm/actions/workflows/build.yml/badge.svg)](https://github.com/surfaceocean/nginx-php-fpm/actions/workflows/build.yml)

## **Links**

- [Docker Hub](https://hub.docker.com/r/surfaceocean/nginx-php-fpm)
- [GitHub Repository](https://github.com/surfaceocean/nginx-php-fpm)

## Images

Images are published to Docker Hub at **`docker.io/<DOCKERHUB_USERNAME>/nginx-php-fpm`** for `linux/amd64` and `linux/arm64`. The workflow uses the account named in the `DOCKERHUB_USERNAME` Actions secret. Examples below use `docker.io/surfaceocean/nginx-php-fpm`; replace `surfaceocean` if your Docker Hub username differs.

| Tag | PHP series |
| --- | --- |
| `8.4` | Latest stable PHP 8.4 patch available in the official PHP base image |
| `8.5` | Latest stable PHP 8.5 patch available in the official PHP base image |
| `latest` | Same image as `8.5` |

Only PHP 8.4 and PHP 8.5 are supported. Legacy Dockerfiles and build targets have been removed.

## Included software

- Nginx, PHP-FPM, ImageMagick, Supervisord, Cron and Composer.
- Node.js 24 LTS and npm.
- PHP extensions: `mysqli`, `mysqlnd`, `pdo_mysql`, `pdo_pgsql`, `apcu`, `sysvsem`, `gmp`, `imagick`, `bz2`, `redis`, `rdkafka`, `memcached`, `zip`, `mbstring`, `exif`, `pcntl`, `bcmath`, `gd`, `intl` and OPcache, alongside the extensions provided by the official PHP image.
- MySQL extensions use the base image's built-in `mysqlnd` driver. OPcache is compiled separately for PHP 8.4 and is built into PHP 8.5.
- PECL extensions are compiled from current stable releases during a fresh build. Exact installed versions are recorded after building.
- PECL channel updates and each extension installation get an initial attempt plus up to three retries, with delays of 10, 20 and 30 seconds before the retries.

## Automated builds and version inventory

[Build workflow](.github/workflows/build.yml) runs:

- Daily at **19:00 UTC**.
- On pushes to `main` that change build inputs.
- Manually through **Actions → Build → Run workflow**.
- On pull requests for validation.

Each PHP version and architecture is built and tested before the release tags are published. If any matrix build job fails, the remaining running or queued matrix jobs are cancelled and release publication is skipped. Scheduled builds refresh the base images and compile extensions without reusing old build layers. Docker Hub publishing uses the existing `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` Actions secrets. The repository's built-in `GITHUB_TOKEN` has `contents: write` only in the publication job so it can commit `versions.json`.

At the end of a successful published build, the workflow generates **[`versions.json`](versions.json) in the repository root** and commits it to `main`. It records actual PHP patch versions, every loaded PHP extension's version, tools, linked libraries and image digests, separately for PHP 8.4/8.5 and each architecture. Values come from the built images. The initial file has no releases until a build has completed. Inventory-only commits do not trigger another build.

The inventory script is also installed in each image:

```sh
docker run --rm --entrypoint php docker.io/surfaceocean/nginx-php-fpm:8.5 \
  /usr/local/bin/build-versions.php
```

Enable GitHub Actions in the repository's **Actions** tab and configure these repository Actions secrets if they are not already present:

- `DOCKERHUB_USERNAME`: the Docker Hub account that owns the `nginx-php-fpm` repository.
- `DOCKERHUB_TOKEN`: a Docker Hub access token with permission to push to that repository.

Push these changes to `main` and run **Build** once if needed. Repository policies must allow the bot's `versions.json` commit. If `main` requires pull requests for every change, configure an appropriate bypass for the publishing bot.

## Run the image

```sh
docker run --rm -p 8080:80 docker.io/surfaceocean/nginx-php-fpm:8.5
```

Open `http://localhost:8080/`. The bundled homepage returns plain text with the current request time in UTC:

```text
Hello World!
2026-10-21 12:34:56 UTC
```

The default Nginx site serves PHP only: `/` runs `index.php`, and requests ending in `.php` run only when that file exists under the document root. Hidden paths, missing scripts, static files, directory URLs, application routes and PHP `PATH_INFO` URLs return 404. Mount a custom site configuration to enable static files or a front controller for your application.

## Docker Compose

```yaml
services:
  app:
    image: docker.io/surfaceocean/nginx-php-fpm:8.5
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./app:/var/www/html
    environment:
      DOCUMENT_ROOT: /var/www/html
      DOMAIN: _
```

Mounting `./app` replaces the bundled application, so provide your own `index.php`. The default document root is `/var/www/html`; `DOMAIN` defaults to `_`. These two environment variables are used only when `/etc/nginx/conf.d/nginx-site.conf` is absent. On first startup, the entrypoint generates that file from the template at `/etc/nginx/templates/nginx-site.conf.template`. An existing site file is loaded without being rewritten, so a read-only mount is supported. The main Nginx configuration sets `client_max_body_size 256m;`, which the default site inherits.

The container runs as `www-data` with UID/GID `1000:1000` by default. Application files must be readable by that user, and directories your application writes to must be writable by it. Keep this user when adding your own configuration.

For Laravel, set `DOCUMENT_ROOT: /var/www/html/public` and mount a custom Nginx site with the static-file and front-controller rules your application needs. The image starts only Nginx and PHP-FPM. To run queue workers, a scheduler or other background services, add their programs to your mounted main Supervisord configuration.

Use `docker compose exec app bash` to open a shell, or `docker compose exec app php artisan migrate` to run migrations in an existing Laravel application.

## Mount your own configuration

Create the configuration files on the host before starting the container. They must be readable by UID `1000`. For example, add these mounts to the Compose service:

```yaml
    volumes:
      - ./app:/var/www/html
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./site.conf:/etc/nginx/conf.d/nginx-site.conf:ro
      - ./supervisord.conf:/etc/supervisor/supervisord.conf:ro
      - ./php.ini:/usr/local/etc/php/conf.d/php.ini:ro
```

Each file replaces a complete configuration at the corresponding path; start from the full defaults below. The `:ro` option protects the mounted configuration from writes inside the container.

| Configuration | Effective path | How to override it |
| --- | --- | --- |
| Nginx main | `/etc/nginx/nginx.conf` | Mount your complete file directly at this path. The image's main file is owned by root. |
| Nginx site | `/etc/nginx/conf.d/nginx-site.conf` | Mount your complete file directly at this path, including with `:ro`. If absent, startup generates it from the root-owned `/etc/nginx/templates/nginx-site.conf.template`, outside the application directory. The image ships the template without an active site file. |
| Supervisord main | `/etc/supervisor/supervisord.conf` | Mount the complete main file directly at this path. Startup loads only this file; the default contains no include section. It does not copy application Supervisor files or generate programs. |
| PHP settings | `/usr/local/etc/php/conf.d/php.ini` | Mount your replacement directly at this path. Other `.ini` files in `conf.d` are still loaded, including extension settings supplied by the PHP base image. |
| OPcache settings | `/usr/local/etc/php/conf.d/opcache.ini` | Mount your replacement directly at this path. |
| PHP-FPM main | `/usr/local/etc/php-fpm.conf` | Mount your replacement directly at this path. The default explicitly includes only `/usr/local/etc/php-fpm.d/www.conf`. |
| PHP-FPM pool | `/usr/local/etc/php-fpm.d/www.conf` | Mount your replacement directly at this path. The default listens on `127.0.0.1:9000`, matching Nginx. |

Restart the container after editing a mounted configuration. Startup retains an existing Nginx site file across restarts. For a generated site stored only in the container's writable layer, recreate the container to apply new environment values. If the site file is persisted in a volume or mounted from the host, edit that file directly. The `/etc/nginx/conf.d` directory is writable by `www-data` so the initial site can be generated. Startup does not copy or import configuration from the application directory.

The main PHP-FPM configuration uses a single explicit include, so additional pool files are loaded only if you change that main configuration.

## Default configuration files

These are the complete default configurations supplied by this repository. The Nginx site block shows the rendered defaults from the shipped template and can be copied to a host file for a direct mount. Only an absent active site is generated with `DOCUMENT_ROOT` and `DOMAIN`; existing files are retained. PHP-FPM and Supervisord load their main files directly.

<details>
<summary>Nginx main: <code>/etc/nginx/nginx.conf</code></summary>

Source: [`src/nginx.conf`](src/nginx.conf)

```nginx
user www-data;
worker_processes auto;
error_log               /var/log/nginx/error.log crit;
pid                     /var/run/nginx.pid;
include /etc/nginx/modules-enabled/*.conf;

events {
	worker_connections  1024;
	use                 epoll;
	multi_accept        on;
}
http {
	sendfile on;
	tcp_nopush on;
	tcp_nodelay on;
	client_header_timeout 3m;
	client_body_timeout 3m;
	client_max_body_size 256m;
	client_header_buffer_size 4k;
	client_body_buffer_size 256k;
	large_client_header_buffers 4 32k;
	send_timeout 3m;
	keepalive_timeout 60 60;
	reset_timedout_connection       on;
	server_names_hash_max_size 1024;
	server_names_hash_bucket_size 1024;
	ignore_invalid_headers on;
	connection_pool_size 256;
	request_pool_size 4k;
	output_buffers 4 32k;
	postpone_output 1460;

	include mime.types;
	default_type application/octet-stream;



	# Compression gzip
	gzip on;
	gzip_vary on;
	gzip_disable "MSIE [1-6]\.";
	gzip_proxied any;
	gzip_min_length 512;
	gzip_comp_level 6;
	gzip_buffers 8 64k;
	gzip_types text/plain text/xml text/css text/js application/x-javascript application/xml image/png image/x-icon image/gif image/jpeg image/svg+xml application/xml+rss text/javascript application/atom+xml application/javascript application/json application/x-font-ttf font/opentype;

	# Proxy settings
	proxy_redirect      off;
	proxy_http_version 1.1;
	proxy_set_header    Host            $host;
	proxy_set_header    X-Real-IP       $remote_addr;
	proxy_set_header    X-Forwarded-For $proxy_add_x_forwarded_for;
	proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_cache_bypass $http_upgrade;
	proxy_pass_header   Set-Cookie;
	proxy_connect_timeout   300;
	proxy_send_timeout  300;
	proxy_read_timeout  300;
	proxy_buffers       32 4k;
	proxy_cache_path /var/cache/nginx levels=2 keys_zone=cache:10m inactive=60m max_size=512m;
	proxy_cache_key "$host$request_uri $cookie_user";
	proxy_temp_path  /var/cache/nginx/temp;
	proxy_ignore_headers Expires Cache-Control;
	proxy_cache_use_stale error timeout invalid_header http_502;
	proxy_cache_valid any 1d;
	server_tokens off;



	include /etc/nginx/conf.d/*.conf;
}
```

</details>

<details>
<summary>Nginx site: <code>/etc/nginx/conf.d/nginx-site.conf</code></summary>

Template source: [`src/nginx-site.conf.template`](src/nginx-site.conf.template), installed at `/etc/nginx/templates/nginx-site.conf.template`. The following configuration uses the default rendered values:

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    root /var/www/html;
    index index.php;

    error_log  /var/log/nginx/error.log;
    access_log /var/log/nginx/access.log;

    server_tokens off;

    fastcgi_hide_header X-Powered-By;
    fastcgi_hide_header X-CF-Powered-By;
    fastcgi_hide_header X-Runtime;

    location ~ (^|/)\. {
        return 404;
    }

    location = / {
        rewrite ^ /index.php last;
    }

    location ~ \.php$ {
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass 127.0.0.1:9000;
    }

    location / {
        return 404;
    }
}
```

</details>

<details>
<summary>Supervisord main: <code>/etc/supervisor/supervisord.conf</code></summary>

Source: [`src/supervisor/supervisord.conf`](src/supervisor/supervisord.conf)

```ini
[supervisord]
nodaemon=true
user=www-data
logfile=/var/log/supervisor/supervisord.log
logfile_maxbytes = 50MB
pidfile=/tmp/supervisord.pid
directory = /tmp


[program:php-fpm]
command=/usr/local/sbin/php-fpm
numprocs=1
autostart=true
autorestart=true
stderr_logfile=/var/log/supervisor/php-fpm.err.log
stdout_logfile=/var/log/supervisor/php-fpm.out.log
user=www-data
priority=1

[program:nginx]
command=/usr/sbin/nginx -g "daemon off;"
numprocs=1
autostart=true
autorestart=true
stderr_logfile=/var/log/nginx/nginx.err.log
stdout_logfile=/var/log/nginx/nginx.out.log
logfile_maxbytes = 50MB
user=www-data
priority=2
```

</details>

<details>
<summary>PHP settings: <code>/usr/local/etc/php/conf.d/php.ini</code></summary>

Source: [`src/php.ini`](src/php.ini)

```ini
date.timezone=UTC
display_errors=Off
log_errors=On
upload_max_filesize= 80M
post_max_size= 80M
memory_limit = 256M
```

</details>

<details>
<summary>OPcache settings: <code>/usr/local/etc/php/conf.d/opcache.ini</code></summary>

Source: [`src/opcache.ini`](src/opcache.ini)

```ini
[opcache]
; Determines if Zend OPCache is enabled
opcache.enable=1

; Determines if Zend OPCache is enabled for the CLI version of PHP
;opcache.enable_cli=1

; The OPcache shared memory storage size.
opcache.memory_consumption=512

; The amount of memory for interned strings in Mbytes.
opcache.interned_strings_buffer=64

; The maximum number of keys (scripts) in the OPcache hash table.
; Only numbers between 200 and 1000000 are allowed.
;If you have multiple PHP sites on the server then consider the value 130986
; for magento 2, keep 65406
opcache.max_accelerated_files=50000

; The maximum percentage of "wasted" memory until a restart is scheduled.
opcache.max_wasted_percentage=15

; When this directive is enabled, the OPcache appends the current working
; directory to the script key, thus eliminating possible collisions between
; files with the same name (basename). Disabling the directive improves
; performance, but may break existing applications.
;opcache.use_cwd=1

; When disabled, you must reset the OPcache manually or restart the
; webserver for changes to the filesystem to take effect.
; For Development / testing, keep 1
; For performance / production, keep 0
opcache.validate_timestamps=0

;opcache.revalidate_freq How often in seconds should the code
;cache expire and check if your code has changed. 0 means it
;checks your PHP code every single request IF YOU HAVE
;opcache.validate_timestamps ENABLED. opcache.validate_timestamps
;should not be enabled by default, as long as it's disabled then any value for opcache.
;revalidate_freq will basically be ignored. You should really only ever enable
;this during development, you don't really want to enable this setting for a production application.
opcache.revalidate_freq=0

; Enables or disables file search in include_path optimization
;opcache.revalidate_path=0

; If disabled, all PHPDoc comments are dropped from the code to reduce the
; size of the optimized code.
opcache.save_comments=1

; If enabled, a fast shutdown sequence is used for the accelerated code
; Depending on the used Memory Manager this may cause some incompatibilities.
opcache.fast_shutdown=1

; Allow file existence override (file_exists, etc.) performance feature.
;opcache.enable_file_override=0

; A bitmask, where each bit enables or disables the appropriate OPcache
; passes
;opcache.optimization_level=0xffffffff

;opcache.inherited_hack=1
;opcache.dups_fix=0

; The location of the OPcache blacklist file (wildcards allowed).
; Each OPcache blacklist file is a text file that holds the names of files
; that should not be accelerated. The file format is to add each filename
; to a new line. The filename may be a full path or just a file prefix
; (i.e., /var/www/x  blacklists all the files and directories in /var/www
; that start with 'x'). Line starting with a ; are ignored (comments).
;opcache.blacklist_filename=

; Allows exclusion of large files from being cached. By default all files
; are cached.
;opcache.max_file_size=0

; Check the cache checksum each N requests.
; The default value of "0" means that the checks are disabled.
;opcache.consistency_checks=0

; How long to wait (in seconds) for a scheduled restart to begin if the cache
; is not being accessed.
;opcache.force_restart_timeout=180

; OPcache error_log file name. Empty string assumes "stderr".
;opcache.error_log=

; All OPcache errors go to the Web server log.
; By default, only fatal errors (level 0) or errors (level 1) are logged.
; You can also enable warnings (level 2), info messages (level 3) or
; debug messages (level 4).
;opcache.log_verbosity_level=1

; Preferred Shared Memory back-end. Leave empty and let the system decide.
;opcache.preferred_memory_model=

; Protect the shared memory from unexpected writing during script execution.
; Useful for internal debugging only.
;opcache.protect_memory=0

; Allows calling OPcache API functions only from PHP scripts which path is
; started from specified string. The default "" means no restriction
;opcache.restrict_api=

; Mapping base of shared memory segments (for Windows only). All the PHP
; processes have to map shared memory into the same address space. This
; directive allows to manually fix the "Unable to reattach to base address"
; errors.
opcache.mmap_base=0x20000000

; Enables and sets the second level cache directory.
; It should improve performance when SHM memory is full, at server restart or
; SHM reset. The default "" disables file based caching.
;opcache.file_cache=

; Enables or disables opcode caching in shared memory.
;opcache.file_cache_only=0

; Enables or disables checksum validation when script loaded from file cache.
;opcache.file_cache_consistency_checks=1

; Implies opcache.file_cache_only=1 for a certain process that failed to
; reattach to the shared memory (for Windows only). Explicitly enabled file
; cache is required.
opcache.file_cache_fallback=1

; Enables or disables copying of PHP code (text segment) into HUGE PAGES.
; This should improve performance, but requires appropriate OS configuration.
;opcache.huge_code_pages=1

; Validate cached file permissions.
; opcache.validate_permission=0

; Prevent name collisions in chroot'ed environment.
; opcache.validate_root=0
```

</details>

<details>
<summary>PHP-FPM main: <code>/usr/local/etc/php-fpm.conf</code></summary>

Source: [`src/php-fpm.conf`](src/php-fpm.conf)

```ini
[global]
daemonize = no
error_log = /proc/self/fd/2
log_limit = 8192

include = /usr/local/etc/php-fpm.d/www.conf
```

</details>

<details>
<summary>PHP-FPM pool: <code>/usr/local/etc/php-fpm.d/www.conf</code></summary>

Source: [`src/php-fpm.d/www.conf`](src/php-fpm.d/www.conf)

```ini
[www]
user = www-data
group = www-data
listen = 127.0.0.1:9000

pm = dynamic
pm.max_children = 5
pm.start_servers = 2
pm.min_spare_servers = 1
pm.max_spare_servers = 3

access.log = /proc/self/fd/2
clear_env = no
catch_workers_output = yes
decorate_workers_output = no
```

</details>

OPcache timestamp validation is disabled by default. Restart the container after deploying changed PHP code, or enable `opcache.validate_timestamps` in your own OPcache configuration during development.

## Local builds

Docker and Python 3.9+ are required; `make` is optional. Run from the repository root:

```sh
bash ./build.sh 8.4
bash ./build.sh 8.5
# Defaults to PHP 8.5; "latest" is also accepted.
bash ./build.sh
# Build both supported versions in order.
make build
```

The script builds for the local Docker platform and updates the corresponding entry in the root `versions.json`. A local build has no published registry digest. `IMAGE_NAME` overrides the default image name and `PYTHON` can select a Python executable:

```sh
IMAGE_NAME=local/nginx-php-fpm bash ./build.sh 8.5
```

For a direct Docker build:

```sh
docker build --pull --build-arg phpVersion=8.5 \
  -f src/docker/Dockerfile -t docker.io/surfaceocean/nginx-php-fpm:8.5 .
```

Use `build.sh` to also update the repository inventory. Both build entry points reject unsupported PHP series.

## Extending the image

```dockerfile
FROM docker.io/surfaceocean/nginx-php-fpm:8.5
COPY --chown=www-data:www-data . /var/www/html
ENV DOCUMENT_ROOT=/var/www/html/public
WORKDIR /var/www/html
```

The original MIT license and copyright are retained in [LICENSE](LICENSE).

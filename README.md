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

## Automated builds and version inventory

[Build workflow](.github/workflows/build.yml) runs:

- Daily at **19:00 UTC**.
- On pushes to `main` that change build inputs.
- Manually through **Actions → Build → Run workflow**.
- On pull requests for validation.

Each PHP version and architecture is built and tested before the release tags are published. Scheduled builds refresh the base images and compile extensions without reusing old build layers. Docker Hub publishing uses the existing `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` Actions secrets. The repository's built-in `GITHUB_TOKEN` has `contents: write` only in the publication job so it can commit `versions.json`.

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
      CLIENT_MAX_BODY_SIZE: 20M
```

For Laravel, set `DOCUMENT_ROOT: /var/www/html/public`. When `/var/www/html/artisan` exists, the entrypoint creates supervisor processes for Laravel's queue worker and scheduler. `LARAVEL_PROCS_NUMBER` controls the number of queue workers (default: `1`).

The default document root is `/var/www/html`. Custom configurations can be mounted at:

- `/var/www/html/conf/nginx/nginx.conf`
- `/var/www/html/conf/nginx/nginx-site.conf`
- `/var/www/html/conf/worker/supervisor.conf`

Use `docker compose exec app bash` to open a shell, or `docker compose exec app php artisan migrate` to run migrations in an existing Laravel application.

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

ARG phpVersion
FROM php:${phpVersion}-fpm-trixie

ARG phpVersion
ARG WORKDIR=/var/www/html
ARG NODE_VERSION=lts.x
ARG GROUP_ID=1000
ARG USER_ID=1000
ARG GROUP_NAME=www-data

ENV DOCUMENT_ROOT=${WORKDIR} \
    DOMAIN=_ \
    NODE_VERSION=${NODE_VERSION} \
    USER_NAME=www-data

COPY php-versions.json /usr/local/share/php-versions.json
COPY src/php-versions.php /usr/local/lib/php-versions.php

# Callers select the base image from the shared configuration before Docker runs.
RUN php -r 'require "/usr/local/lib/php-versions.php"; assertSupportedPhpSeries($argv[1]); if ((PHP_MAJOR_VERSION . "." . PHP_MINOR_VERSION) !== $argv[1]) { throw new RuntimeException("Base image PHP does not match the selected version."); }' "$phpVersion"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        apt-utils \
        ca-certificates \
        cron \
        curl \
        gettext-base \
        git \
        imagemagick \
        libbz2-dev \
        libfreetype-dev \
        libgmp-dev \
        libicu-dev \
        libjpeg62-turbo-dev \
        libmagickwand-dev \
        libmemcached-dev \
        libonig-dev \
        libpng-dev \
        libpq-dev \
        librdkafka-dev \
        libsasl2-dev \
        libtool \
        libxml2-dev \
        libzip-dev \
        nano \
        nginx \
        procps \
        psmisc \
        sqlite3 \
        sudo \
        supervisor \
        unzip \
        zip \
        zlib1g-dev; \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}" -o /tmp/nodesource_setup.sh; \
    bash /tmp/nodesource_setup.sh; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -f /tmp/nodesource_setup.sh; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# mysqlnd and mbstring are already compiled into the official PHP base image.
# Build both MySQL APIs against mysqlnd instead of a separate client library.
RUN set -eux; \
    php -r 'exit(extension_loaded("mysqlnd") ? 0 : 1);'; \
    docker-php-ext-configure gd --with-freetype --with-jpeg; \
    docker-php-ext-configure mysqli --with-mysqli=mysqlnd; \
    docker-php-ext-configure pdo_mysql --with-pdo-mysql=mysqlnd; \
    extensions="bcmath bz2 exif gd gmp intl mysqli pcntl pdo_mysql pdo_pgsql sysvsem zip"; \
    if ! php -r 'exit(extension_loaded("mbstring") ? 0 : 1);'; then \
        extensions="$extensions mbstring"; \
    fi; \
    if ! php -r 'exit(extension_loaded("Zend OPcache") ? 0 : 1);'; then \
        extensions="$extensions opcache"; \
    fi; \
    docker-php-ext-install -j"$(nproc)" $extensions

# Build OPcache only when the selected base image does not already provide it.
COPY --from=ghcr.io/php/pie:bin /pie /usr/local/bin/pie

# Resolve the latest compatible stable extension releases on each fresh build.
# Retry each network operation up to ten times after its initial attempt.
RUN set -eux; \
    retry() { \
        max_retries=10; \
        retries=0; \
        until "$@"; do \
            status=$?; \
            if [ "$retries" -ge "$max_retries" ]; then \
                echo "Command failed after $max_retries retries: $*" >&2; \
                return "$status"; \
            fi; \
            retries=$((retries + 1)); \
            echo "Retry $retries/$max_retries in $((retries * 10)) seconds: $*" >&2; \
            sleep "$((retries * 10))"; \
        done; \
    }; \
    for package in apcu/apcu imagick/imagick php-memcached/php-memcached phpredis/phpredis rdkafka/rdkafka; do \
        retry pie install --no-interaction --no-cache --skip-enable-extension "$package" </dev/null; \
    done; \
    docker-php-ext-enable apcu imagick memcached redis rdkafka

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer
COPY src/build-versions.php /usr/local/bin/build-versions.php

# Fail the build if any required module is missing or a tool cannot run.
RUN php /usr/local/bin/build-versions.php >/usr/local/share/build-versions.json

WORKDIR ${WORKDIR}

RUN rm -rf /var/www/* && mkdir -p /var/www/html

COPY src/index.php ${WORKDIR}/index.php
COPY src/php.ini ${PHP_INI_DIR}/conf.d/
COPY src/opcache.ini ${PHP_INI_DIR}/conf.d/
COPY src/php-fpm.conf /usr/local/etc/php-fpm.conf
COPY src/php-fpm.d/www.conf /usr/local/etc/php-fpm.d/www.conf
COPY src/supervisor/supervisord.conf /etc/supervisor/supervisord.conf

COPY src/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    ln -s /usr/local/bin/entrypoint.sh /

RUN rm -f /etc/nginx/conf.d/default.conf \
        /etc/nginx/sites-enabled/default \
        /etc/nginx/sites-available/default \
        /etc/nginx/nginx.conf

COPY src/nginx.conf /etc/nginx/nginx.conf
COPY src/nginx-site.conf.template /etc/nginx/templates/nginx-site.conf.template

RUN usermod -u ${USER_ID} ${USER_NAME} && \
    groupmod -g ${GROUP_ID} ${GROUP_NAME}

RUN set -eux; \
    mkdir -p /var/log/supervisor /var/cache/nginx \
        /var/lib/nginx /var/run/supervisor; \
    chown -R "${USER_NAME}:${GROUP_NAME}" \
        "${WORKDIR}" /var/cache/nginx /var/lib/nginx; \
    chown root:root /var/log /var/log/supervisor \
        /etc/nginx/conf.d /var/run/supervisor /tmp; \
    chmod 755 /var/log /var/log/supervisor \
        /etc/nginx/conf.d /var/run/supervisor; \
    chmod 1777 /tmp; \
    for path in /var/run/nginx.pid /var/run/supervisor/supervisord.pid \
        /var/log/supervisor/supervisord.log \
        /var/log/supervisor/php-fpm.err.log \
        /var/log/supervisor/php-fpm.out.log \
        /var/log/supervisor/nginx.err.log /var/log/supervisor/nginx.out.log; do \
        touch "$path"; \
        chown "root:$(id -g "${USER_NAME}")" "$path"; \
        chmod 660 "$path"; \
    done

# Start as root; the default Supervisord configuration drops to www-data.
USER root
EXPOSE 80
ENTRYPOINT ["entrypoint.sh"]

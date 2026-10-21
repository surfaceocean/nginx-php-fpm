ARG phpVersion=8.5
FROM php:${phpVersion}-fpm-trixie

ARG phpVersion
ARG WORKDIR=/var/www/html
ARG NODE_VERSION=24.x
ARG GROUP_ID=1000
ARG USER_ID=1000
ARG GROUP_NAME=www-data

ENV DOCUMENT_ROOT=${WORKDIR} \
    LARAVEL_PROCS_NUMBER=1 \
    DOMAIN=_ \
    CLIENT_MAX_BODY_SIZE=15M \
    NODE_VERSION=${NODE_VERSION} \
    USER_NAME=www-data

# Only the maintained PHP release lines are supported by this Dockerfile.
RUN case "$phpVersion" in \
        8.4|8.5) ;; \
        *) echo "Unsupported PHP version: $phpVersion (use 8.4 or 8.5)" >&2; exit 1 ;; \
    esac

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        cron \
        curl \
        git \
        imagemagick \
        libbz2-dev \
        libfreetype6-dev \
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
        libxml2-dev \
        libzip-dev \
        nano \
        nginx \
        openssh-server \
        sqlite3 \
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

# PHP 8.5 includes OPcache itself; the conditional above builds it only for 8.4.
# Resolve the latest stable PECL releases on each uncached daily build.
RUN set -eux; \
    pecl channel-update pecl.php.net; \
    pecl config-set preferred_state stable; \
    pecl install apcu imagick memcached redis rdkafka </dev/null; \
    docker-php-ext-enable apcu imagick memcached redis rdkafka; \
    rm -rf /tmp/pear

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer
COPY src/build-versions.php /usr/local/bin/build-versions.php

# Fail the build if any required module is missing or a tool cannot run.
RUN php /usr/local/bin/build-versions.php >/usr/local/share/build-versions.json

WORKDIR ${WORKDIR}

RUN rm -rf /var/www/* && mkdir -p /var/www/html

COPY src/index.php ${WORKDIR}/index.php
COPY src/php.ini ${PHP_INI_DIR}/conf.d/
COPY src/opcache.ini ${PHP_INI_DIR}/conf.d/
COPY src/supervisor/supervisord.conf /etc/supervisor/supervisord.conf

COPY src/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    ln -s /usr/local/bin/entrypoint.sh /

RUN rm -f /etc/nginx/conf.d/default.conf \
        /etc/nginx/sites-enabled/default \
        /etc/nginx/sites-available/default \
        /etc/nginx/nginx.conf

COPY src/nginx.conf /etc/nginx/nginx.conf
COPY src/default.conf /etc/nginx/conf.d/

RUN usermod -u ${USER_ID} ${USER_NAME} && \
    groupmod -g ${GROUP_ID} ${GROUP_NAME}

RUN mkdir -p /var/log/supervisor /var/log/nginx /var/cache/nginx && \
    touch /var/run/nginx.pid && \
    chown -R ${USER_NAME}:${GROUP_NAME} \
        /var/www \
        /var/log \
        /etc/supervisor/conf.d \
        ${PHP_INI_DIR}/conf.d \
        /var/cache/nginx \
        /var/lib/nginx \
        /etc/nginx/nginx.conf \
        /var/run/nginx.pid \
        /etc/nginx/conf.d \
        /tmp

EXPOSE 80
ENTRYPOINT ["entrypoint.sh"]

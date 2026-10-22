#!/bin/sh

set -e

info() { echo "[INFO] $*"; }

NGINX_SITE_CONF="/etc/nginx/conf.d/nginx-site.conf"
NGINX_SITE_TEMPLATE="/etc/nginx/templates/nginx-site.conf.template"

if [ -e "$NGINX_SITE_CONF" ] || [ -L "$NGINX_SITE_CONF" ]; then
  if [ ! -f "$NGINX_SITE_CONF" ] || [ ! -r "$NGINX_SITE_CONF" ]; then
    echo "[ERROR] $NGINX_SITE_CONF must be a readable configuration file" >&2
    exit 1
  fi
  info "Using existing $NGINX_SITE_CONF"
else
  export DOCUMENT_ROOT="${DOCUMENT_ROOT:-/var/www/html}"
  export DOMAIN="${DOMAIN:-_}"

  temporary=$(mktemp /etc/nginx/conf.d/.nginx-site.XXXXXX)
  trap 'rm -f -- "$temporary"' 0
  envsubst '${DOMAIN} ${DOCUMENT_ROOT}' \
    < "$NGINX_SITE_TEMPLATE" > "$temporary"
  chmod 644 "$temporary"
  mv -fT -- "$temporary" "$NGINX_SITE_CONF"
  trap - 0
  info "Generated default $NGINX_SITE_CONF"
fi

# Root Supervisor removes its PID on shutdown; restore the fixed writable file
# inside its protected directory before the default Supervisor drops privileges.
if [ "$(id -u)" -eq 0 ]; then
  touch /var/run/supervisor/supervisord.pid
  chown "root:$(id -g www-data)" /var/run/supervisor/supervisord.pid
  chmod 660 /var/run/supervisor/supervisord.pid
fi

exec supervisord -c /etc/supervisor/supervisord.conf

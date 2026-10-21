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

exec supervisord -c /etc/supervisor/supervisord.conf

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

if [ "$(id -u)" -eq 0 ]; then
  application_gid=$(id -g www-data)
  # Repair existing and rotated logs, including files in mounted directories.
  chown "root:$application_gid" /var/log/supervisor
  chmod 2755 /var/log/supervisor
  find /var/log/supervisor -type f \
    -exec chown "root:$application_gid" {} + -exec chmod 660 {} +

  # Root Supervisor removes its PID on shutdown; restore it before dropping privileges.
  touch /var/run/supervisor/supervisord.pid
  chown "root:$application_gid" /var/run/supervisor/supervisord.pid
  chmod 660 /var/run/supervisor/supervisord.pid
fi

# Preserve group write access for logs created by foreground Supervisor.
umask 0007
exec supervisord -c /etc/supervisor/supervisord.conf

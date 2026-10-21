#!/usr/bin/env bash
set -euo pipefail

image=${1:?Usage: test-image.sh IMAGE PHP_SERIES PLATFORM}
php_series=${2:?PHP series is required}
platform=${3:?Platform is required}

case "$php_series" in 8.4|8.5) ;; *) echo 'Only PHP 8.4 and 8.5 are supported' >&2; exit 1 ;; esac
case "$platform" in linux/amd64|linux/arm64) ;; *) echo 'Unsupported platform' >&2; exit 1 ;; esac
actual_platform=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
[[ "$actual_platform" == "$platform" ]]
[[ $(docker image inspect "$image" --format '{{.Config.User}}') == root ]]
[[ $(docker run --rm --platform "$platform" --entrypoint id "$image" -u) == 0 ]]

# Exercise the compiled modules without depending on external databases or caches.
docker run --rm -i --platform "$platform" --entrypoint php "$image" \
  -d apc.enable_cli=1 /dev/stdin "$php_series" <<'PHP'
<?php
function check(bool $ok, string $message): void {
    if (!$ok) { fwrite(STDERR, $message . PHP_EOL); exit(1); }
}
check(str_starts_with(PHP_VERSION, $argv[1] . '.'), 'Incorrect PHP release');
$required = ['mysqli', 'mysqlnd', 'pdo_mysql', 'apcu', 'sysvsem', 'gmp',
    'imagick', 'bz2', 'redis', 'pdo_pgsql', 'rdkafka', 'memcached', 'zip',
    'mbstring', 'exif', 'pcntl', 'bcmath', 'gd', 'intl', 'Zend OPcache'];
foreach ($required as $extension) {
    check(extension_loaded($extension), 'Missing extension: ' . $extension);
}
check(str_contains(mysqli_get_client_info(), 'mysqlnd'), 'MySQL must use mysqlnd');
check(in_array('mysql', PDO::getAvailableDrivers(), true), 'PDO MySQL driver missing');
check(in_array('pgsql', PDO::getAvailableDrivers(), true), 'PDO PostgreSQL driver missing');
check(gmp_strval(gmp_add('12345678901234567890', '1')) === '12345678901234567891', 'GMP failed');
check(bzdecompress(bzcompress('build smoke test')) === 'build smoke test', 'Bzip2 failed');
check(apcu_store('build-smoke', 'ok') && apcu_fetch('build-smoke') === 'ok', 'APCu failed');
$semaphore = sem_get(random_int(1, 2147483646));
check($semaphore !== false && sem_acquire($semaphore), 'SysV semaphore failed');
check(sem_release($semaphore) && sem_remove($semaphore), 'SysV semaphore cleanup failed');
$imagick = new Imagick();
$imagick->newImage(1, 1, 'white', 'png');
check(strlen($imagick->getImageBlob()) > 0, 'ImageMagick PNG encoding failed');
$imagick->clear();
check(new Redis() instanceof Redis, 'Redis initialization failed');
check(new Memcached() instanceof Memcached, 'Memcached initialization failed');
check(new RdKafka\Conf() instanceof RdKafka\Conf, 'Rdkafka initialization failed');
echo 'Compiled extension smoke tests passed', PHP_EOL;
PHP

docker run --rm --platform "$platform" --entrypoint nginx "$image" -t
docker run --rm --platform "$platform" --entrypoint php-fpm "$image" -t
docker run --rm --platform "$platform" --entrypoint sh "$image" -eu -c '
  [ ! -e /etc/nginx/conf.d/nginx-site.conf ]
  [ ! -L /etc/nginx/conf.d/nginx-site.conf ]
'

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container="nginx-php-fpm-smoke-${RANDOM}-$$"
mounted_container="${container}-mounted"
env_container="${container}-env"
invalid_container="${container}-invalid"
test_dir=$(mktemp -d)
cleanup() {
  status=$?
  for name in "$container" "$mounted_container" "$env_container" "$invalid_container"; do
    if (( status != 0 )); then docker logs "$name" 2>&1 || true; fi
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
  rm -rf -- "$test_dir"
  exit "$status"
}
trap cleanup EXIT

wait_for_http() {
  local name=$1 path=$2
  for attempt in {1..90}; do
    if docker exec "$name" curl --fail --silent --max-time 2 "http://127.0.0.1$path" \
      > "$test_dir/response.json" 2>/dev/null; then return 0; fi
    [[ $(docker inspect "$name" --format '{{.State.Running}}') == true ]] || break
    sleep 1
  done
  echo "HTTP readiness failed: $name$path" >&2
  return 1
}

expect_status() {
  local path=$1 expected=$2 name=${3:-$container} actual
  actual=$(docker exec "$name" curl --silent --show-error --max-time 5 \
    --output /dev/null --write-out '%{http_code}' "http://127.0.0.1$path")
  [[ "$actual" =~ ^($expected)$ ]] || {
    echo "$path: expected HTTP $expected, got $actual" >&2
    return 1
  }
}

mkdir -p "$test_dir/app/nested" "$test_dir/app/.git" "$test_dir/app/conf/worker" \
  "$test_dir/app/conf/nginx" "$test_dir/supervisor/conf.d"
cat > "$test_dir/app/__build_probe.php" <<'PHP'
<?php
header('Content-Type: application/json');
echo json_encode(['php' => PHP_VERSION, 'sapi' => PHP_SAPI,
    'query' => $_GET['probe'] ?? '', 'precision' => ini_get('precision'),
    'pool' => getenv('SMOKE_POOL') ?: '']);
PHP
cp "$test_dir/app/__build_probe.php" "$test_dir/app/nested/allowed.php"
cp "$test_dir/app/__build_probe.php" "$test_dir/app/.hidden.php"
cp "$test_dir/app/__build_probe.php" "$test_dir/app/.git/x.php"
for path in .env notes.txt composer.json style.css index.html nested/config.conf; do
  printf 'smoke-private-content\n' > "$test_dir/app/$path"
done
cat > "$test_dir/app/artisan" <<'PHP'
<?php file_put_contents('/tmp/smoke-legacy-artisan', 'unexpected execution');
PHP
cat > "$test_dir/app/conf/worker/supervisor.conf" <<'CONF'
[program:smoke-legacy-worker]
command=/usr/bin/touch /tmp/smoke-legacy-worker
priority=0
startsecs=0
autorestart=false
stdout_logfile=/tmp/smoke-legacy-worker.log
redirect_stderr=true
CONF
sed 's/smoke-legacy-worker/smoke-legacy-include/g' \
  "$test_dir/app/conf/worker/supervisor.conf" > "$test_dir/supervisor/conf.d/legacy.conf"
cp "$repo_root/src/nginx.conf" "$test_dir/app/conf/nginx/nginx.conf"
printf '\n# smoke-legacy-nginx-main\n' >> "$test_dir/app/conf/nginx/nginx.conf"
cat > "$test_dir/app/conf/nginx/nginx-site.conf" <<'CONF'
# smoke-legacy-nginx-site
server {
    listen 80 default_server;
    root /var/www/html;
    location = /legacy-nginx-sentinel { return 200 "smoke-legacy-nginx-site\n"; }
    location / { try_files $uri =404; }
}
CONF

docker create --platform "$platform" --name "$container" "$image" >/dev/null
docker cp "$test_dir/app/." "$container:/var/www/html"
# docker cp seeds these as root before startup; runtime code must ignore them.
docker cp "$test_dir/supervisor/." "$container:/etc/supervisor"
docker start "$container" >/dev/null
wait_for_http "$container" '/__build_probe.php?probe=runtime-check'

grep -F "\"php\":\"${php_series}." "$test_dir/response.json"
grep -F '"sapi":"fpm-fcgi"' "$test_dir/response.json"
grep -F '"query":"runtime-check"' "$test_dir/response.json"
expect_status /nested/allowed.php 200
for path in / /index.php; do
  docker exec "$container" curl --fail --silent --show-error --max-time 5 \
    --dump-header /tmp/smoke-headers "http://127.0.0.1$path" > "$test_dir/greeting.txt"
  # Validate the complete response, including newlines, without normalizing HTML.
  docker exec -i "$container" php -r '
    $body = stream_get_contents(STDIN);
    exit(preg_match("/\\AHello World!\\n[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC\\n\\z/", $body) ? 0 : 1);
  ' < "$test_dir/greeting.txt"
  docker exec "$container" grep -Eiq '^Content-Type: text/plain([;[:space:]]|$)' /tmp/smoke-headers
done
for path in /notes.txt /composer.json /style.css /index.html /nested/config.conf \
  /artisan /nested/ /missing-route /missing.php /__build_probe.php/path \
  /legacy-nginx-sentinel /conf/nginx/nginx.conf /conf/nginx/nginx-site.conf; do
  expect_status "$path" 404
done
for path in /.env /.hidden.php /.git/x.php; do
  expect_status "$path" 404
done

docker exec --user www-data "$container" sh -eu -c '
  [ "$(id -u)" = 1000 ]
  # PID 1 must have dropped privileges; docker exec defaults to the image user.
  for field in Uid Gid; do
    grep -Eq "^$field:[[:space:]]+1000[[:space:]]+1000[[:space:]]+1000[[:space:]]+1000[[:space:]]*$" /proc/1/status
  done
  for path in /etc/supervisor /etc/supervisor/supervisord.conf /etc/nginx \
    /etc/nginx/nginx.conf /etc/nginx/templates /etc/nginx/templates/nginx-site.conf.template \
    /usr/local/etc /usr/local/etc/php /usr/local/etc/php/conf.d \
    /usr/local/etc/php-fpm.conf /usr/local/etc/php-fpm.d /usr/local/etc/php-fpm.d/www.conf; do
    [ "$(stat -c %u "$path")" = 0 ] && [ ! -w "$path" ] || {
      echo "Runtime can alter protected configuration: $path" >&2; exit 1;
    }
  done
  [ -z "$(find /usr/local/etc/php /usr/local/etc/php-fpm.d \( ! -user root -o -writable \) -print -quit)" ]
  ! grep -Eiq "^[[:space:]]*\\[include\\]" /etc/supervisor/supervisord.conf
  includes=$(sed -n "s/^[[:space:]]*include[[:space:]]*=[[:space:]]*//p" /usr/local/etc/php-fpm.conf)
  [ "$includes" = /usr/local/etc/php-fpm.d/www.conf ]
  grep -Eq "^[[:space:]]*listen[[:space:]]*=[[:space:]]*127[.]0[.]0[.]1:9000[[:space:]]*$" /usr/local/etc/php-fpm.d/www.conf
  for marker in /tmp/smoke-legacy-artisan /tmp/smoke-legacy-worker /tmp/smoke-legacy-include; do
    [ ! -e "$marker" ] || { echo "Legacy configuration executed: $marker" >&2; exit 1; }
  done
  ! grep -Fq "smoke-legacy-nginx-main" /etc/nginx/nginx.conf
  ! grep -Fq "smoke-legacy-nginx-site" /etc/nginx/conf.d/nginx-site.conf
'
docker exec "$container" nginx -t
docker exec "$container" php-fpm -t

# An existing active site must survive restart without regeneration or replacement.
docker exec "$container" sh -eu -c '
  printf "\n# smoke-restart-preserved\n" >> /etc/nginx/conf.d/nginx-site.conf
'
site_inode=$(docker exec "$container" stat -c %i /etc/nginx/conf.d/nginx-site.conf)
docker cp "$container:/etc/nginx/conf.d/nginx-site.conf" "$test_dir/site-before.conf"
docker stop "$container" >/dev/null
docker start "$container" >/dev/null
wait_for_http "$container" /__build_probe.php
docker cp "$container:/etc/nginx/conf.d/nginx-site.conf" "$test_dir/site-after.conf"
cmp "$test_dir/site-before.conf" "$test_dir/site-after.conf"
[[ $(docker exec "$container" stat -c %i /etc/nginx/conf.d/nginx-site.conf) == "$site_inode" ]]

# A readable file symlink is another supported active configuration path.
docker exec "$container" sh -eu -c '
  mv /etc/nginx/conf.d/nginx-site.conf /tmp/smoke-readable-nginx-site.conf
  ln -s /tmp/smoke-readable-nginx-site.conf /etc/nginx/conf.d/nginx-site.conf
'
docker stop "$container" >/dev/null
docker start "$container" >/dev/null
wait_for_http "$container" /__build_probe.php
[[ $(docker exec "$container" readlink /etc/nginx/conf.d/nginx-site.conf) == /tmp/smoke-readable-nginx-site.conf ]]
docker cp "$container:/tmp/smoke-readable-nginx-site.conf" "$test_dir/site-symlink-after.conf"
cmp "$test_dir/site-before.conf" "$test_dir/site-symlink-after.conf"

# Bind all supported configuration entry points read-only in one fresh container.
cat > "$test_dir/site.conf" <<'CONF'
# smoke-mounted-nginx-site
server {
    listen 80 default_server;
    server_name _;
    root /var/www/html;
    location = /style.css { try_files $uri =404; }
    location = /__build_probe.php {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass 127.0.0.1:9000;
    }
    location / { return 404; }
}
CONF
cp "$repo_root/src/nginx.conf" "$test_dir/nginx.conf"
printf '\n# smoke-mounted-nginx-main\n' >> "$test_dir/nginx.conf"
cp "$repo_root/src/supervisor/supervisord.conf" "$test_dir/supervisord.conf"
cat >> "$test_dir/supervisord.conf" <<'CONF'

[program:smoke-mounted-supervisor]
command=/usr/bin/touch /tmp/smoke-mounted-supervisor
priority=0
startsecs=0
autorestart=false
stdout_logfile=/tmp/smoke-mounted-supervisor.log
redirect_stderr=true
CONF
printf 'precision=12\n' > "$test_dir/smoke.ini"
cp "$repo_root/src/php-fpm.conf" "$test_dir/php-fpm.conf"
printf '\n[global]\nprocess.max = 16\n' >> "$test_dir/php-fpm.conf"
cp "$repo_root/src/php-fpm.d/www.conf" "$test_dir/www.conf"
printf '\nenv[SMOKE_POOL] = mounted\n' >> "$test_dir/www.conf"
chmod 644 "$test_dir/"*.conf "$test_dir/smoke.ini"
docker create --platform "$platform" --name "$mounted_container" \
  --mount "type=bind,src=$test_dir/nginx.conf,dst=/etc/nginx/nginx.conf,readonly" \
  --mount "type=bind,src=$test_dir/site.conf,dst=/etc/nginx/conf.d/nginx-site.conf,readonly" \
  --mount "type=bind,src=$test_dir/supervisord.conf,dst=/etc/supervisor/supervisord.conf,readonly" \
  --mount "type=bind,src=$test_dir/smoke.ini,dst=/usr/local/etc/php/conf.d/zz-smoke.ini,readonly" \
  --mount "type=bind,src=$test_dir/php-fpm.conf,dst=/usr/local/etc/php-fpm.conf,readonly" \
  --mount "type=bind,src=$test_dir/www.conf,dst=/usr/local/etc/php-fpm.d/www.conf,readonly" \
  "$image" >/dev/null
docker cp "$test_dir/app/__build_probe.php" "$mounted_container:/var/www/html/__build_probe.php"
docker cp "$test_dir/app/style.css" "$mounted_container:/var/www/html/style.css"
docker start "$mounted_container" >/dev/null
wait_for_http "$mounted_container" /style.css
[[ $(cat "$test_dir/response.json") == smoke-private-content ]]
wait_for_http "$mounted_container" /__build_probe.php
grep -F '"sapi":"fpm-fcgi"' "$test_dir/response.json"
grep -F '"precision":"12"' "$test_dir/response.json"
grep -F '"pool":"mounted"' "$test_dir/response.json"
docker exec "$mounted_container" test -f /tmp/smoke-mounted-supervisor
docker exec "$mounted_container" php-fpm -tt > "$test_dir/fpm-config.txt" 2>&1
grep -F 'process.max = 16' "$test_dir/fpm-config.txt"
docker exec "$mounted_container" nginx -T > "$test_dir/mounted-nginx.conf" 2>&1
grep -F 'smoke-mounted-nginx-main' "$test_dir/mounted-nginx.conf"
grep -F 'smoke-mounted-nginx-site' "$test_dir/mounted-nginx.conf"
docker exec --user www-data "$mounted_container" sh -eu -c '
  [ ! -w /etc/nginx/nginx.conf ]
  [ ! -w /etc/nginx/conf.d/nginx-site.conf ]
'
docker stop "$mounted_container" >/dev/null
docker start "$mounted_container" >/dev/null
wait_for_http "$mounted_container" /style.css
[[ $(cat "$test_dir/response.json") == smoke-private-content ]]
wait_for_http "$mounted_container" /__build_probe.php
grep -F '"precision":"12"' "$test_dir/response.json"
grep -F '"pool":"mounted"' "$test_dir/response.json"
docker cp "$mounted_container:/etc/nginx/nginx.conf" "$test_dir/mounted-main-after.conf"
docker cp "$mounted_container:/etc/nginx/conf.d/nginx-site.conf" "$test_dir/mounted-site-after.conf"
cmp "$test_dir/nginx.conf" "$test_dir/mounted-main-after.conf"
cmp "$test_dir/site.conf" "$test_dir/mounted-site-after.conf"

# Render only the supported environment placeholders and retain Nginx variables.
mkdir -p "$test_dir/public"
cp "$test_dir/app/__build_probe.php" "$test_dir/public/__build_probe.php"
cp "$test_dir/app/notes.txt" "$test_dir/public/notes.txt"
docker create --platform "$platform" --name "$env_container" \
  --env DOCUMENT_ROOT=/var/www/html/public --env DOMAIN=example.test "$image" >/dev/null
docker cp "$test_dir/public" "$env_container:/var/www/html/public"
docker start "$env_container" >/dev/null
wait_for_http "$env_container" '/__build_probe.php?probe=env-check'
grep -F '"query":"env-check"' "$test_dir/response.json"
expect_status /notes.txt 404 "$env_container"
docker cp "$env_container:/etc/nginx/conf.d/nginx-site.conf" "$test_dir/env-site.conf"
grep -Eq '^[[:space:]]*server_name example[.]test;[[:space:]]*$' "$test_dir/env-site.conf"
grep -Eq '^[[:space:]]*root /var/www/html/public;[[:space:]]*$' "$test_dir/env-site.conf"
grep -F 'try_files $uri =404;' "$test_dir/env-site.conf"
grep -F 'fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;' "$test_dir/env-site.conf"
docker exec "$env_container" nginx -t

# A dangling active-site symlink must be rejected without touching it or its target.
docker create --platform "$platform" --name "$invalid_container" --entrypoint sh \
  "$image" -eu -c '
    site=/etc/nginx/conf.d/nginx-site.conf
    target=/tmp/smoke-missing-nginx-site.conf
    ln -s "$target" "$site"
    set +e
    timeout 10 entrypoint.sh
    status=$?
    set -e
    [ "$status" -ne 0 ] && [ "$status" -ne 124 ]
    [ -L "$site" ]
    [ "$(readlink "$site")" = "$target" ]
    [ ! -e "$target" ]
  ' >/dev/null
docker start "$invalid_container" >/dev/null
[[ $(docker wait "$invalid_container") == 0 ]]

echo "Runtime and compiled extension smoke tests passed for PHP $php_series on $platform"

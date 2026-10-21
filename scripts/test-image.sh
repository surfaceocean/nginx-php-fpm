#!/usr/bin/env bash
set -euo pipefail

image=${1:?Usage: test-image.sh IMAGE PHP_SERIES PLATFORM}
php_series=${2:?PHP series is required}
platform=${3:?Platform is required}

case "$php_series" in 8.4|8.5) ;; *) echo 'Only PHP 8.4 and 8.5 are supported' >&2; exit 1 ;; esac
case "$platform" in linux/amd64|linux/arm64) ;; *) echo 'Unsupported platform' >&2; exit 1 ;; esac
actual_platform=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
[[ "$actual_platform" == "$platform" ]]

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

container="nginx-php-fpm-smoke-${RANDOM}-$$"
test_dir=$(mktemp -d)
cleanup() {
  status=$?
  if (( status != 0 )); then docker logs "$container" 2>&1 || true; fi
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -f "$test_dir/probe.php" "$test_dir/response.json"
  rmdir "$test_dir"
  exit "$status"
}
trap cleanup EXIT

cat > "$test_dir/probe.php" <<'PHP'
<?php
header('Content-Type: application/json');
echo json_encode(['php' => PHP_VERSION, 'sapi' => PHP_SAPI]);
PHP
docker run -d --platform "$platform" --name "$container" "$image" >/dev/null
docker cp "$test_dir/probe.php" "$container:/var/www/html/__build_probe.php"

ready=false
for attempt in {1..90}; do
  if docker exec "$container" curl --fail --silent http://127.0.0.1/__build_probe.php \
    > "$test_dir/response.json" 2>/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
[[ "$ready" == true ]]
grep -F "\"php\":\"${php_series}." "$test_dir/response.json"
grep -F '"sapi":"fpm-fcgi"' "$test_dir/response.json"
docker exec "$container" nginx -t
docker exec "$container" php-fpm -t
echo "Nginx to PHP-FPM HTTP smoke test passed for PHP $php_series on $platform"

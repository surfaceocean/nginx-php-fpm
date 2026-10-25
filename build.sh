#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [ "$#" -gt 1 ]; then
    echo "Usage: ./build.sh [PHP_SERIES|latest]" >&2
    exit 1
fi

python_command="${PYTHON:-}"
if [ -z "$python_command" ]; then
    for candidate in python3 python; do
        if "$candidate" -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1; then
            python_command="$candidate"
            break
        fi
    done
fi
if [ -z "$python_command" ]; then
    echo "Python 3.9+ is required to update versions.json (or set PYTHON)." >&2
    exit 1
fi

php_version=$("$python_command" scripts/php_versions.py --check "${1:-latest}")
latest_php=$("$python_command" scripts/php_versions.py --latest)

image_name="${IMAGE_NAME:-docker.io/surfaceocean/nginx-php-fpm}"
image="$image_name:$php_version"
docker build --pull --build-arg "phpVersion=$php_version" \
    -f src/docker/Dockerfile -t "$image" .
if [ "$php_version" = "$latest_php" ]; then
    docker tag "$image" "$image_name:latest"
fi

manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
docker run --rm --entrypoint php "$image" /usr/local/bin/build-versions.php > "$manifest"
platform="$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"
"$python_command" scripts/update-versions.py --local-manifest "$manifest" \
    --php "$php_version" --platform "$platform" --image "$image_name" --output versions.json

echo "Built $image and updated versions.json."

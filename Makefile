SHELL := bash
IMAGE_NAME ?= docker.io/surfaceocean/nginx-php-fpm
PYTHON ?= python3
export IMAGE_NAME PYTHON

.PHONY: all build FORCE
all: build

# Run serially so every build can safely update the shared versions.json.
build:
	@set -eu; versions=$$("$(PYTHON)" scripts/php_versions.py --list); \
	for version in $$versions; do bash ./build.sh "$$version"; done

# Examples: make build-8.5 or make build-latest.
build-%: FORCE
	bash ./build.sh "$*"

FORCE:

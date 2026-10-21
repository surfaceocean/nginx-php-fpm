IMAGE_NAME ?= docker.io/surfaceocean/nginx-php-fpm
export IMAGE_NAME

.PHONY: all build build-84 build-85
all: build

# Run serially so both builds can safely update the shared versions.json.
build:
	bash ./build.sh 8.4
	bash ./build.sh 8.5

build-84:
	bash ./build.sh 8.4

build-85:
	bash ./build.sh 8.5

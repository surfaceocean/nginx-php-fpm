#!/usr/bin/env python3
"""Record real image versions and publish only a complete, tested build matrix."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

# Also support imports by the standalone test runner.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from php_versions import load_versions


PHP_SERIES = load_versions(Path(__file__).resolve().parents[1] / "php-versions.json")
PLATFORMS = ("linux/amd64", "linux/arm64")
IMAGE = "docker.io/surfaceocean/nginx-php-fpm"
REQUIRED_EXTENSIONS = {
    "apcu", "bcmath", "bz2", "exif", "gd", "gmp", "imagick", "intl",
    "mbstring", "memcached", "mysqli", "mysqlnd", "opcache", "pcntl",
    "pdo", "pdo_mysql", "pdo_pgsql", "rdkafka", "redis", "sysvsem", "zip",
}


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)


def check_digest(digest, required=False):
    if digest in (None, "") and not required:
        return None
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Expected an immutable registry digest (sha256 plus 64 hex characters)")
    return digest


def check_runtime(runtime, series):
    if series not in PHP_SERIES:
        raise ValueError("Supported PHP series: " + ", ".join(PHP_SERIES))
    if not isinstance(runtime, dict) or not re.fullmatch(
        re.escape(series) + r"\.\d+", str(runtime.get("php", ""))
    ):
        raise ValueError(f"The image must contain a stable PHP {series} patch release")
    extensions = runtime.get("extensions")
    if not isinstance(extensions, dict):
        raise ValueError("Missing actual PHP extension versions")
    normalized = {name.lower(): value for name, value in extensions.items()}
    missing = REQUIRED_EXTENSIONS - normalized.keys()
    if missing:
        raise ValueError("Required PHP extensions are missing: " + ", ".join(sorted(missing)))
    invalid = {
        name for name in REQUIRED_EXTENSIONS
        if not isinstance(normalized[name], str) or not normalized[name].strip()
    }
    if invalid:
        raise ValueError("Required PHP extension versions are missing: " + ", ".join(sorted(invalid)))
    tools = runtime.get("tools", {})
    if not isinstance(tools, dict) or any(
        not isinstance(tools.get(name), str) or not tools[name].strip()
        for name in ("nginx", "composer", "node", "npm")
    ):
        raise ValueError("Missing actual nginx, Composer, Node.js or npm versions")
    return runtime


def make_record(manifest, series, platform, image, digest=None):
    if platform not in PLATFORMS:
        raise ValueError("Only linux/amd64 and linux/arm64 are supported")
    return {
        "php_series": series,
        "platform": platform,
        "image": image,
        "digest": check_digest(digest),
        "runtime": check_runtime(manifest, series),
    }


def collect_records(directory, image):
    records = {}
    for path in sorted(Path(directory).glob("*.json")):
        value = read_json(path)
        record = make_record(
            value["runtime"], value["php_series"], value["platform"],
            value["image"], value.get("digest"),
        )
        key = (record["php_series"], record["platform"])
        if record["image"] != image:
            raise ValueError(f"{path}: image name differs from this build")
        if key in records:
            raise ValueError(f"Duplicate architecture manifest: {key}")
        records[key] = record
    expected = {(series, platform) for series in PHP_SERIES for platform in PLATFORMS}
    if set(records) != expected:
        raise ValueError(f"Incomplete build matrix; missing {sorted(expected - set(records))}")
    for series in PHP_SERIES:
        versions = {records[(series, platform)]["runtime"]["php"] for platform in PLATFORMS}
        if len(versions) != 1:
            raise ValueError(f"PHP {series} patch versions differ across architectures; rebuild")
    return records


def publish_release(image, series, records):
    patch = records[0]["runtime"]["php"]
    tags = [f"{image}:{series}", f"{image}:php{series}", f"{image}:{patch}"]
    if series == PHP_SERIES[-1]:
        tags.append(f"{image}:latest")
    references = [f"{image}@{check_digest(record['digest'], required=True)}" for record in records]
    command = ["docker", "buildx", "imagetools", "create"]
    for tag in tags:
        command.extend(("--tag", tag))
    subprocess.run(command + references, check=True)
    descriptor = json.loads(subprocess.check_output(
        ["docker", "buildx", "imagetools", "inspect", tags[0], "--format", "{{json .Manifest}}"],
        text=True,
    ))
    # Check the registry result, rather than inferring a manifest digest locally.
    actual = {
        f"{item['platform']['os']}/{item['platform']['architecture']}": item["digest"]
        for item in descriptor.get("manifests", [])
    }
    expected = {record["platform"]: record["digest"] for record in records}
    if actual != expected:
        raise ValueError(f"Published manifest does not match the tested images: {actual}")
    return tags, check_digest(descriptor.get("digest"), required=True)


def document(records, image, source=None, publish=False):
    releases = {}
    # Validate every digest before changing any supported registry tags.
    if publish:
        for record in records.values():
            check_digest(record["digest"], required=True)
    for series in PHP_SERIES:
        selected = [record for (php, _), record in sorted(records.items()) if php == series]
        if not selected:
            continue
        tags, digest = publish_release(image, series, selected) if publish else ([], None)
        releases[series] = {
            "php": selected[0]["runtime"]["php"],
            "published": publish,
            "tags": tags,
            "digest": digest,
            "platforms": {
                record["platform"]: {**record["runtime"], "digest": record["digest"]}
                for record in selected
            },
        }
    return {
        "schema_version": 1,
        "image": image,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": source,
        "releases": releases,
    }


def local_document(record, image, output):
    """Keep other observed builds, but invalidate release-level publication data."""
    series = record["php_series"]
    result = document({(series, record["platform"]): record}, image)
    if not output.exists():
        return result
    previous = read_json(output)
    if not isinstance(previous, dict) or previous.get("schema_version") != 1:
        raise ValueError("Existing versions.json has an unsupported schema")
    if previous.get("image") != image:
        return result
    if not isinstance(previous.get("releases"), dict):
        raise ValueError("Existing versions.json has no releases object")
    releases = {
        php: release for php, release in previous["releases"].items() if php in PHP_SERIES
    }
    if any(not isinstance(release, dict) for release in releases.values()):
        raise ValueError("Existing versions.json has an invalid release object")
    platforms = releases.get(series, {}).get("platforms", {})
    if not isinstance(platforms, dict):
        raise ValueError("Existing release has an invalid platforms object")
    current = result["releases"][series]
    current["platforms"] = {
        **{platform: value for platform, value in platforms.items() if platform in PLATFORMS},
        **current["platforms"],
    }
    releases[series] = current
    result["releases"] = {php: releases[php] for php in PHP_SERIES if php in releases}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record-manifest", type=Path, help="Wrap one image's runtime manifest for CI")
    mode.add_argument("--local-manifest", type=Path, help="Update versions.json with one local image")
    mode.add_argument("--records", type=Path, help="Combine the complete CI architecture manifests")
    parser.add_argument("--php", choices=PHP_SERIES)
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--digest", default=None)
    parser.add_argument("--output", type=Path, default=Path("versions.json"))
    parser.add_argument("--publish", action="store_true", help="Publish tested multiarch release aliases to Docker Hub")
    parser.add_argument("--source-sha")
    parser.add_argument("--source-repository")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    args = parser.parse_args()
    try:
        if args.publish and not args.records:
            raise ValueError("Publishing requires a complete CI build matrix")
        if args.records:
            records = collect_records(args.records, args.image)
            source = {
                "repository": args.source_repository,
                "commit": args.source_sha,
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
            }
            result = document(records, args.image, source, args.publish)
        else:
            if not args.php or not args.platform:
                raise ValueError("--php and --platform are required for a single image manifest")
            record = make_record(
                read_json(args.record_manifest or args.local_manifest),
                args.php, args.platform, args.image, args.digest,
            )
            result = record if args.record_manifest else local_document(record, args.image, args.output)
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot record build versions: {error}\n")


if __name__ == "__main__":
    main()

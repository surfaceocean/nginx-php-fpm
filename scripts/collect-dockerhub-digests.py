#!/usr/bin/env python3
"""Recover known Docker Hub manifest digests from committed version inventories."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
OBJECT_ID = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')


class HistoryError(Exception):
    """An error safe to print without Git output or inventory contents."""


def validate_image(image):
    if not isinstance(image, str) or not re.fullmatch(
        r'docker\.io/([a-z0-9][a-z0-9_-]*)/([a-z0-9]+(?:[._-][a-z0-9]+)*)', image):
        raise HistoryError('Expected docker.io/NAMESPACE/REPOSITORY without a tag')


def git_output(arguments, run=None):
    """Read Git output using argument arrays; never expose command error details."""
    try:
        result = (run or subprocess.run)(['git', *arguments], shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            text=True, encoding='utf-8')
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise HistoryError('Unable to read the local Git history') from None
    if result.returncode != 0 or not isinstance(result.stdout, str):
        raise HistoryError('Unable to read the local Git history')
    return result.stdout


def inventory_digests(value, image):
    """Accept only this repository's published root and platform manifest digests."""
    if not isinstance(value, dict):
        raise HistoryError('Historical version inventory must be a JSON object')
    if value.get('image') != image:
        return set()
    if value.get('schema_version') != 1 or not isinstance(value.get('releases'), dict):
        raise HistoryError('Invalid historical version inventory schema')
    values = set()
    for release in value['releases'].values():
        if not isinstance(release, dict):
            raise HistoryError('Invalid historical release record')
        if release.get('published') is not True:
            continue
        platforms = release.get('platforms')
        if not isinstance(platforms, dict) or not platforms:
            raise HistoryError('Published historical release has no platform records')
        records = [release]
        for platform, runtime in platforms.items():
            if not isinstance(platform, str) or not platform or not isinstance(runtime, dict):
                raise HistoryError('Invalid historical platform record')
            records.append(runtime)
        for record in records:
            manifest = record.get('digest')
            if not isinstance(manifest, str) or not DIGEST.fullmatch(manifest):
                raise HistoryError('Invalid or missing historical manifest digest')
            values.add(manifest)
    return values


def collect_digests(image, run=None):
    """Scan every versions.json revision reachable from HEAD, once per Git blob."""
    validate_image(image)
    shallow = git_output(['rev-parse', '--is-shallow-repository'], run).strip()
    if shallow != 'false':
        raise HistoryError('Complete Git history is required; use checkout fetch-depth: 0')
    commits = git_output(['log', '--format=%H', '--full-history', '-m',
        '--diff-filter=AM', 'HEAD', '--', 'versions.json'], run).splitlines()
    seen_commits, seen_blobs, values = set(), set(), set()
    for commit in commits:
        if not OBJECT_ID.fullmatch(commit):
            raise HistoryError('Invalid historical commit identifier')
        if commit in seen_commits:
            continue
        seen_commits.add(commit)
        blob = git_output(['rev-parse', commit + ':versions.json'], run).strip()
        if not OBJECT_ID.fullmatch(blob):
            raise HistoryError('Invalid historical inventory identifier')
        if blob in seen_blobs:
            continue
        seen_blobs.add(blob)
        source = git_output(['cat-file', 'blob', blob], run)
        try:
            value = json.loads(source)
        except (ValueError, RecursionError):
            raise HistoryError('Historical version inventory contains invalid JSON') from None
        values.update(inventory_digests(value, image))
    return sorted(values)


def write_inventory(image, output, run=None):
    inventory = {'schema_version': 1, 'image': image, 'digests': collect_digests(image, run)}
    try:
        Path(output).write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')
    except OSError:
        raise HistoryError('Unable to write the historical digest inventory') from None
    return inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = write_inventory(args.image, args.output)
    except HistoryError as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
    print(f'Recovered {len(result["digests"])} known manifest digests from Git history')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

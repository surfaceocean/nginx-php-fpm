#!/usr/bin/env python3
"""Prune obsolete Docker Hub tags and unreferenced manifests after publication."""
import argparse
import base64
from dataclasses import dataclass
import heapq
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

HUB = 'https://hub.docker.com'
REGISTRY = 'https://registry-1.docker.io'
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
TAG = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z')
CANDIDATE = re.compile(r'build-([0-9]+)-([0-9]+)-([0-9]+\.[0-9]+)-(amd64|arm64)\Z')
PLATFORMS = {'linux/amd64', 'linux/arm64'}
ACCEPT = ', '.join(('application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json'))

class CleanupError(Exception):
    """Sanitized error: never include remote response bodies or credentials."""


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise CleanupError('Invalid or missing immutable digest')
    return value


def image_parts(image):
    if not isinstance(image, str) or not re.fullmatch(
        r'docker\.io/([a-z0-9][a-z0-9_-]*)/([a-z0-9]+(?:[._-][a-z0-9]+)*)', image):
        raise CleanupError('Expected docker.io/NAMESPACE/REPOSITORY without a tag')
    return image[len('docker.io/'):].split('/', 1)


def validate_versions(value, image, previous=False):
    image_parts(image)
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('image') != image:
        raise CleanupError('Inventory schema or image does not match this build')
    releases = value.get('releases')
    if not isinstance(releases, dict) or (not releases and not previous):
        raise CleanupError('Inventory has no published releases')
    # Initial inventories and observations from unpublished builds have no old release to prune.
    if previous:
        releases = {k: v for k, v in releases.items() if isinstance(v, dict) and v.get('published') is True}
        value = {**value, 'releases': releases}
    seen = set()
    for series, release in releases.items():
        if not isinstance(series, str) or not re.fullmatch(r'[0-9]+\.[0-9]+', series) or not isinstance(release, dict):
            raise CleanupError('Invalid PHP release')
        php = release.get('php')
        if not isinstance(php, str) or not re.fullmatch(re.escape(series) + r'\.[0-9]+', php):
            raise CleanupError('PHP patch differs from its release series')
        if release.get('published') is not True:
            raise CleanupError('Every current release must be published before cleanup')
        digest(release.get('digest'))
        tags = release.get('tags')
        required = {f'{image}:{series}', f'{image}:php{series}', f'{image}:{php}'}
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise CleanupError('Missing published aliases')
        if len(tags) != len(set(tags)) or not required.issubset(tags):
            raise CleanupError('Missing or duplicate published aliases')
        for tag in tags:
            if not tag.startswith(image + ':') or not TAG.fullmatch(tag[len(image) + 1:]) or tag in seen:
                raise CleanupError('Invalid, foreign or duplicate published alias')
            seen.add(tag)
        platforms = release.get('platforms')
        if not isinstance(platforms, dict) or set(platforms) != PLATFORMS:
            raise CleanupError('Both amd64 and arm64 must be published')
        for runtime in platforms.values():
            if not isinstance(runtime, dict) or runtime.get('php') != php:
                raise CleanupError('Architecture PHP differs from its release')
            digest(runtime.get('digest'))
    if not previous:
        source = value.get('source')
        if not isinstance(source, dict) or any(not isinstance(source.get(k), str)
            or not re.fullmatch(r'[1-9][0-9]*', source[k]) for k in ('run_id', 'run_attempt')):
            raise CleanupError('Publishing run ID and attempt are required')
    return value


def aliases(versions):
    return {tag.rsplit(':', 1)[1]: release for release in versions['releases'].values() for tag in release['tags']}


def old_tags(tags, versions):
    keep = aliases(versions)
    run = tuple(int(versions['source'][k]) for k in ('run_id', 'run_attempt'))
    selected = []
    for tag in tags:
        if tag in keep:
            continue
        candidate = CANDIDATE.fullmatch(tag)
        # Retain every release/custom tag; collect managed candidates even from retired series.
        if candidate and (int(candidate[1]), int(candidate[2])) <= run:
            selected.append(tag)
    return sorted(selected)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    def json(self):
        try:
            return json.loads(self.body.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise CleanupError('Docker returned invalid JSON') from None


class Http:
    def __init__(self, opener=None, sleep=time.sleep, attempts=3):
        self.opener, self.sleep, self.attempts = opener or build_opener(NoRedirect()), sleep, attempts
    def request(self, method, url, headers=None, payload=None, allowed=(), label='Docker API request'):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        headers = {'Accept': 'application/json', **(headers or {})}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        for attempt in range(self.attempts):
            retry_after = None
            try:
                with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=30) as reply:
                    return Response(reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read())
            except HTTPError as error:
                status = error.code
                if status in allowed:
                    return Response(status, {k.lower(): v for k, v in error.headers.items()}, b'')
                if status not in (429, 500, 502, 503, 504):
                    hint = '; verify a PAT with Repo Read, Write, Delete permission' if status in (401, 403) else ''
                    raise CleanupError(f'{label} failed (HTTP {status}){hint}') from None
                retry_after = error.headers.get('Retry-After')
            except (URLError, TimeoutError, OSError):
                status = 'network timeout or connection error'
            if attempt + 1 == self.attempts:
                raise CleanupError(f'{label} failed after {self.attempts} attempts ({status})') from None
            self.sleep(min(int(retry_after), 30) if retry_after and retry_after.isdigit() else 2 ** attempt)


class Hub:
    def __init__(self, image, username, secret, http):
        namespace, repository = image_parts(image)
        self.http = http
        token = http.request('POST', HUB + '/v2/auth/token', payload={
            'identifier': username, 'secret': secret}, label='Docker Hub authentication').json()
        if not isinstance(token, dict) or not isinstance(token.get('access_token'), str) or not token['access_token']:
            raise CleanupError('Docker Hub did not return an access token')
        self.headers = {'Authorization': 'Bearer ' + token['access_token']}
        self.list_path = f'/v2/namespaces/{namespace}/repositories/{repository}/tags'
        self.legacy_path = f'/v2/repositories/{namespace}/{repository}/tags'
    def checked_page(self, url):
        p = urlsplit(url)
        if p.scheme != 'https' or p.netloc != 'hub.docker.com' or p.fragment or p.path.rstrip('/') not in {
            self.list_path, self.legacy_path}:
            raise CleanupError('Refusing pagination outside this Docker Hub repository')
        return url
    def list_tags(self):
        page, visited, tags = HUB + self.list_path + '?page_size=100&page=1', set(), set()
        while page:
            page = self.checked_page(page)
            if page in visited or len(visited) >= 10000:
                raise CleanupError('Looping or excessive Docker Hub pagination')
            visited.add(page)
            result = self.http.request('GET', page, self.headers, label='List Docker Hub tags').json()
            if not isinstance(result, dict) or not isinstance(result.get('results'), list):
                raise CleanupError('Invalid Docker Hub tag page')
            for item in result['results']:
                name = item.get('name') if isinstance(item, dict) else None
                if not isinstance(name, str) or not TAG.fullmatch(name):
                    raise CleanupError('Invalid Docker Hub tag name')
                tags.add(name)
            next_page = result.get('next')
            if next_page is not None and not isinstance(next_page, str):
                raise CleanupError('Invalid Docker Hub pagination')
            page = self.checked_page(urljoin(page, next_page)) if next_page else None
        return tags
    def delete_tag(self, tag):
        if not TAG.fullmatch(tag):
            raise CleanupError('Invalid deletion tag')
        # Tag-only endpoint used by Docker's own hub-tool; never substitute a digest DELETE here.
        response = self.http.request('DELETE', HUB + self.legacy_path + '/' + quote(tag, safe='') + '/',
            self.headers, allowed=(404,), label='Delete Docker Hub tag ' + tag)
        return response.status != 404


@dataclass
class Manifest:
    digest: str
    children: tuple
    platforms: dict
    is_index: bool


class Registry:
    def __init__(self, image, username, secret, http):
        self.repository = '/'.join(image_parts(image))
        self.http, self.cache = http, {}
        basic = base64.b64encode((username + ':' + secret).encode('utf-8')).decode('ascii')
        self.auth_headers = {'Authorization': 'Basic ' + basic}
        self.authenticate()
    def authenticate(self):
        result = self.http.request('GET', 'https://auth.docker.io/token?' + urlencode({
            'service': 'registry.docker.io', 'scope': f'repository:{self.repository}:pull,push,delete'}),
            self.auth_headers, label='Docker registry authentication').json()
        token = (result.get('token') or result.get('access_token')) if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise CleanupError('Docker registry did not return an access token')
        self.token, self.headers = token, {'Authorization': 'Bearer ' + token, 'Accept': ACCEPT}
    def request(self, method, url, allowed=(), label='Docker registry request'):
        response = self.http.request(method, url, self.headers, allowed=(*allowed, 401), label=label)
        if response.status == 401:
            # A history sweep may outlast a short-lived registry token. Refresh once.
            self.authenticate()
            if method == 'DELETE':
                self.require_delete()
            response = self.http.request(method, url, self.headers, allowed=allowed, label=label)
        return response
    def require_delete(self):
        # Token received over TLS directly from auth.docker.io. This grant check is
        # advisory; the registry authenticates and authorizes every actual DELETE.
        try:
            part = self.token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            granted = any(x.get('type') == 'repository' and x.get('name') == self.repository
                and isinstance(x.get('actions'), list) and 'delete' in x['actions']
                for x in claims.get('access', []) if isinstance(x, dict))
        except (ValueError, IndexError, TypeError, AttributeError):
            granted = False
        if not granted:
            raise CleanupError('Registry deletion requires a Docker Hub PAT with Repo Read, Write, Delete permission')
    def manifest(self, reference, missing_ok=False):
        if DIGEST.fullmatch(reference) and reference in self.cache:
            return self.cache[reference]
        if not DIGEST.fullmatch(reference) and not TAG.fullmatch(reference):
            raise CleanupError('Invalid manifest reference')
        response = self.request('GET', REGISTRY + '/v2/' + self.repository + '/manifests/' + quote(reference, safe=':'),
            allowed=(404,), label='Read Docker manifest')
        if response.status == 404:
            if missing_ok:
                return None
            raise CleanupError('A retained tag or manifest disappeared during cleanup planning')
        actual = digest(response.headers.get('docker-content-digest'))
        if DIGEST.fullmatch(reference) and actual != reference:
            raise CleanupError('Registry returned a different manifest digest')
        value = response.json()
        if not isinstance(value, dict) or value.get('schemaVersion') != 2:
            raise CleanupError('Unsupported manifest format; refuse cleanup')
        children, platforms, index = set(), {}, 'manifests' in value
        if index:
            if not isinstance(value['manifests'], list) or not value['manifests']:
                raise CleanupError('Image index has no valid manifests')
            for item in value['manifests']:
                if not isinstance(item, dict):
                    raise CleanupError('Invalid image index descriptor')
                child, platform = digest(item.get('digest')), item.get('platform', {})
                children.add(child)
                if not isinstance(platform, dict):
                    raise CleanupError('Invalid image platform')
                if platform.get('os') and platform.get('architecture'):
                    key = platform['os'] + '/' + platform['architecture']
                    # Historical indexes can have several attestations (unknown/unknown)
                    # or platform variants. Preserve all children; ambiguous platforms
                    # must never satisfy verify_current's exact two-platform comparison.
                    platforms[key] = None if key in platforms else child
        if 'subject' in value:
            if not isinstance(value['subject'], dict):
                raise CleanupError('Invalid OCI subject')
            children.add(digest(value['subject'].get('digest')))
        result = Manifest(actual, tuple(sorted(children)), platforms, index)
        self.cache[actual] = result
        return result
    def delete_manifest(self, value):
        digest(value)
        return self.request('DELETE', REGISTRY + '/v2/' + self.repository + '/manifests/' + value,
            allowed=(403, 404), label='Delete unreferenced Docker manifest').status


def verify_current(registry, versions, sleep=time.sleep, attempts=3):
    for attempt in range(attempts):
        matches = True
        for tag, release in aliases(versions).items():
            actual = registry.manifest(tag, missing_ok=True)
            expected = {p: r['digest'] for p, r in release['platforms'].items()}
            if actual is None or actual.digest != release['digest'] or actual.platforms != expected:
                matches = False
                break
        if matches:
            return
        if attempt + 1 < attempts:
            sleep(2 ** attempt)
    raise CleanupError('Published tags no longer match this build; refuse cleanup of a stale or inconsistent publication')


def graph_from_roots(registry, roots, missing_ok=False):
    graph, pending, seen = {}, list(roots), set()
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.add(item)
        manifest = registry.manifest(item, missing_ok=missing_ok)
        if manifest:
            graph[item] = manifest
            pending.extend(manifest.children)
        if len(seen) > 10000:
            raise CleanupError('Excessive manifest graph; refuse cleanup')
    return graph


def deletion_order(graph):
    # A shared child must follow every candidate parent, not merely the first one.
    incoming = {key: 0 for key in graph}
    for manifest in graph.values():
        for child in manifest.children:
            if child in incoming:
                incoming[child] += 1
    ready = [(not graph[k].is_index, k) for k, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, item = heapq.heappop(ready)
        order.append(item)
        for child in graph[item].children:
            if child in incoming:
                incoming[child] -= 1
                if incoming[child] == 0:
                    heapq.heappush(ready, (not graph[child].is_index, child))
    if len(order) != len(graph):
        raise CleanupError('Cyclic manifest references; refuse cleanup')
    return order


@dataclass
class Plan:
    tags: list
    manifests: dict
    order: list
    protected: set


def plan_cleanup(hub, registry, versions, previous=None, sleep=time.sleep, known_digests=()):
    verify_current(registry, versions, sleep=sleep)
    tags = hub.list_tags()
    selected = old_tags(tags, versions)
    protected_roots = {d for release in versions['releases'].values()
        for d in [release['digest'], *(r['digest'] for r in release['platforms'].values())]}
    candidates = set(known_digests)
    for tag in sorted(tags):
        manifest = registry.manifest(tag, missing_ok=tag in selected)
        if manifest:
            (candidates if tag in selected else protected_roots).add(manifest.digest)
    if previous:
        for release in previous['releases'].values():
            candidates.update([release['digest'], *(r['digest'] for r in release['platforms'].values())])
    protected = set(graph_from_roots(registry, protected_roots))
    graph = {d: m for d, m in graph_from_roots(registry, candidates, missing_ok=True).items() if d not in protected}
    return Plan(selected, graph, deletion_order(graph), protected)


def cleanup(hub, registry, versions, previous=None, delete=False, sleep=time.sleep, emit=print, known_digests=()):
    image = versions.get('image') if isinstance(versions, dict) else None
    versions = validate_versions(versions, image)
    if previous is not None:
        previous = validate_versions(previous, image, previous=True)
    known_digests = {digest(value) for value in known_digests}
    if delete:
        registry.require_delete()
    plan = plan_cleanup(hub, registry, versions, previous, sleep=sleep, known_digests=known_digests)
    for tag in plan.tags:
        emit(('Delete tag: ' if delete else 'Would delete tag: ') + tag)
    for item in plan.order:
        emit('Old manifest candidate: ' + item)
    if not delete:
        emit(f'Dry run: {len(plan.tags)} obsolete tags and {len(plan.order)} old manifest candidates; nothing deleted')
        return {'tags': 0, 'manifests': 0, 'retained': 0}
    verify_current(registry, versions, sleep=sleep)
    deleted_tags = sum(bool(hub.delete_tag(tag)) for tag in plan.tags)
    verify_current(registry, versions, sleep=sleep)
    # Another publisher may have added a tag after our plan was assembled.
    # Refresh every remaining reference before GC, including future build tags.
    remaining_roots = set()
    for tag in sorted(hub.list_tags()):
        manifest = registry.manifest(tag, missing_ok=True)
        if manifest is not None:  # Hub's tag listing can lag behind a successful deletion.
            remaining_roots.add(manifest.digest)
    protected = plan.protected | set(graph_from_roots(registry, remaining_roots))
    verify_current(registry, versions, sleep=sleep)
    deleted, retained, blocked = 0, 0, set()
    for item in plan.order:
        if item in protected or item in blocked:
            retained += 1
            continue
        status = registry.delete_manifest(item)
        if status == 403:
            emit('Keep registry-referenced or policy-protected manifest (HTTP 403): ' + item)
            retained += 1
            pending = list(plan.manifests[item].children)
            while pending:
                child = pending.pop()
                if child not in blocked:
                    blocked.add(child)
                    if child in plan.manifests:
                        pending.extend(plan.manifests[child].children)
        elif status != 404:
            deleted += 1
    verify_current(registry, versions, sleep=sleep)
    emit(f'Cleanup complete: {deleted_tags} tags and {deleted} manifests deleted; {retained} referenced/protected manifests kept')
    return {'tags': deleted_tags, 'manifests': deleted, 'retained': retained}


def read_known_digests(path, image):
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('image') != image or not isinstance(value.get('digests'), list)):
        raise CleanupError('Historical digest inventory does not match this repository')
    return {digest(item) for item in value['digests']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--versions', type=Path, default=Path('versions.json'))
    parser.add_argument('--previous-versions', type=Path)
    parser.add_argument('--known-digests', type=Path, help='Additional recorded historical manifest digests')
    parser.add_argument('--delete', action='store_true', help='Apply cleanup; otherwise only report the plan')
    args = parser.parse_args(argv)
    try:
        versions = validate_versions(json.loads(args.versions.read_text(encoding='utf-8')), args.image)
        previous = None
        if args.previous_versions:
            previous = validate_versions(json.loads(args.previous_versions.read_text(encoding='utf-8')), args.image, previous=True)
        known_digests = read_known_digests(args.known_digests, args.image) if args.known_digests else set()
        username, secret = os.environ.get('DOCKERHUB_USERNAME', ''), os.environ.get('DOCKERHUB_TOKEN', '')
        if not username or not secret:
            raise CleanupError('DOCKERHUB_USERNAME and DOCKERHUB_TOKEN must be provided through the environment')
        http = Http()
        cleanup(Hub(args.image, username, secret, http), Registry(args.image, username, secret, http),
            versions, previous, delete=args.delete, known_digests=known_digests)
    except CleanupError as error:
        parser.exit(1, f'Cannot clean Docker Hub: {error}\n')
    except (OSError, ValueError):
        parser.exit(1, 'Cannot clean Docker Hub: unable to read a valid version inventory\n')


if __name__ == '__main__':
    main()

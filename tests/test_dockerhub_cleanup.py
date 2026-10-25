"""Exercise Docker Hub cleanup without network access or destructive API calls."""
import base64
from copy import deepcopy
from io import BytesIO, StringIO
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cleanup_dockerhub', ROOT / 'scripts/cleanup-dockerhub.py')
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
IMAGE = 'docker.io/surfaceocean/nginx-php-fpm'
SECRET = 'secret-that-must-never-appear-in-logs'


def sha(number):
    return 'sha256:' + f'{number:064x}'


def inventory():
    releases = {}
    for number, series in enumerate(('8.4', '8.5'), start=1):
        releases[series] = {'php': series + '.5', 'published': True, 'digest': sha(number * 10),
            'tags': [IMAGE + ':' + t for t in (series, 'php' + series, series + '.5')],
            'platforms': {platform: {'php': series + '.5', 'digest': sha(number * 10 + index)}
                for index, platform in enumerate(('linux/amd64', 'linux/arm64'), start=1)}}
    releases['8.5']['tags'].append(IMAGE + ':latest')
    return {'schema_version': 1, 'image': IMAGE, 'source': {'run_id': '100', 'run_attempt': '2'}, 'releases': releases}


def jwt(actions=('pull', 'push', 'delete')):
    body = {'access': [{'type': 'repository', 'name': 'surfaceocean/nginx-php-fpm', 'actions': list(actions)}]}
    return 'header.' + base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=') + '.signature'


class FakeRegistry:
    def __init__(self, versions):
        self.nodes, self.tags, self.deleted, self.reads = {}, {}, [], []
        self.blocked, self.permission = set(), True
        for release in versions['releases'].values():
            platforms = {p: r['digest'] for p, r in release['platforms'].items()}
            self.add_index(release['digest'], platforms)
            self.tags.update({t.rsplit(':', 1)[1]: release['digest'] for t in release['tags']})
    def add_leaf(self, value):
        self.nodes.setdefault(value, cleanup.Manifest(value, (), {}, False))
    def add_index(self, value, platforms):
        self.nodes[value] = cleanup.Manifest(value, tuple(sorted(set(platforms.values()))), platforms, True)
        for child in platforms.values():
            self.add_leaf(child)
    def manifest(self, reference, missing_ok=False):
        self.reads.append(reference)
        value = self.nodes.get(self.tags.get(reference, reference))
        if value is None and not missing_ok:
            raise cleanup.CleanupError('Missing retained manifest')
        return value
    def require_delete(self):
        if not self.permission:
            raise cleanup.CleanupError('Repo Read, Write, Delete permission required')
    def delete_manifest(self, value):
        self.deleted.append(value)
        if value in self.blocked or value in self.tags.values() or any(value in n.children for n in self.nodes.values()):
            return 403
        if value not in self.nodes:
            return 404
        del self.nodes[value]
        return 202


class FakeHub:
    def __init__(self, registry):
        self.registry, self.deleted, self.after_delete = registry, [], None
    def list_tags(self):
        return set(self.registry.tags)
    def delete_tag(self, tag):
        self.deleted.append(tag)
        self.registry.tags.pop(tag, None)
        if self.after_delete:
            self.after_delete()
        return True


class CleanupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.versions = inventory()
        self.registry = FakeRegistry(self.versions)
        self.hub = FakeHub(self.registry)
        self.messages = []
    def run_cleanup(self, delete=True, previous=None, known_digests=()):
        return cleanup.cleanup(self.hub, self.registry, self.versions, previous, delete,
            sleep=lambda _: None, emit=self.messages.append, known_digests=known_digests)
    def old_index(self, tag='build-90-1-8.4-amd64', number=30):
        platforms = {'linux/amd64': sha(number + 1), 'linux/arm64': sha(number + 2)}
        self.registry.add_index(sha(number), platforms)
        self.registry.tags[tag] = sha(number)
        return sha(number), set(platforms.values())
    def test_inventory_requires_complete_published_releases(self):
        mutations = [lambda x: x.update(schema_version=2), lambda x: x.update(image='docker.io/other/image'),
            lambda x: x['releases']['8.4'].update(published=False),
            lambda x: x['releases']['8.4']['tags'].pop(),
            lambda x: x['releases']['8.4']['platforms'].pop('linux/arm64'),
            lambda x: x['releases']['8.4']['platforms']['linux/arm64'].update(php='8.4.99'),
            lambda x: x['releases']['8.4'].update(digest='latest'),
            lambda x: x['source'].update(run_id='not-a-run')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = deepcopy(self.versions)
                mutate(value)
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.validate_versions(value, IMAGE)
    def test_first_unpublished_inventory_has_no_previous_release(self):
        for releases in ({}, {'8.4': {'published': False, 'digest': None}}):
            value = cleanup.validate_versions({'schema_version': 1, 'image': IMAGE, 'releases': releases}, IMAGE, previous=True)
            self.assertEqual(value['releases'], {})
            self.assertEqual(self.run_cleanup(previous=value)['manifests'], 0)
    def test_only_managed_build_tags_selected_and_all_patch_tags_retained(self):
        tags = {'8.4.4', '8.4.5', '8.4.6', '8.3.1', '8.6.1', 'latest', 'php8.4', 'custom',
            'build-90-1-8.4-amd64', 'build-100-2-8.5-arm64', 'build-100-3-8.5-arm64',
            'build-101-1-8.4-amd64', 'build-90-1-8.6-amd64', 'build-custom'}
        self.assertEqual(cleanup.old_tags(tags, self.versions), ['build-100-2-8.5-arm64', 'build-90-1-8.4-amd64', 'build-90-1-8.6-amd64'])
    def test_new_php_series_is_derived_from_inventory(self):
        value = deepcopy(self.versions)
        release = value['releases'].pop('8.4')
        release['php'] = '8.6.5'
        release['tags'] = [IMAGE + ':' + t for t in ('8.6', 'php8.6', '8.6.5')]
        for runtime in release['platforms'].values():
            runtime['php'] = '8.6.5'
        value['releases']['8.6'] = release
        cleanup.validate_versions(value, IMAGE)
        self.assertEqual(cleanup.old_tags({'8.4.4', '8.6.4', 'build-90-1-8.6-arm64'}, value), ['build-90-1-8.6-arm64'])
    def test_old_index_precedes_its_platforms_and_current_platforms_are_protected(self):
        old, children = self.old_index()
        current = self.versions['releases']['8.4']['platforms']['linux/amd64']['digest']
        self.registry.tags['build-100-2-8.4-amd64'] = current
        result = self.run_cleanup()
        self.assertEqual(result['tags'], 2)
        self.assertEqual(set(self.registry.deleted), {old, *children})
        self.assertEqual(self.registry.deleted[0], old)
        self.assertNotIn(current, self.registry.deleted)
    def test_failed_candidate_is_cleaned_and_future_or_custom_references_survive(self):
        old, children = self.old_index('build-90-1-8.4-arm64')
        self.registry.tags['8.4.99'] = self.registry.tags['build-90-1-8.4-arm64']
        self.run_cleanup()
        self.assertEqual(self.hub.deleted, ['build-90-1-8.4-arm64'])
        self.assertEqual(self.registry.deleted, [])
        self.assertIn(old, self.registry.nodes)
    def test_previous_untagged_release_is_reclaimed(self):
        previous = deepcopy(self.versions)
        old = previous['releases']['8.4']
        old['digest'] = sha(30)
        old['platforms']['linux/amd64']['digest'] = sha(31)
        old['platforms']['linux/arm64']['digest'] = sha(32)
        self.registry.add_index(sha(30), {p: r['digest'] for p, r in old['platforms'].items()})
        result = self.run_cleanup(previous=previous)
        self.assertEqual(result['manifests'], 3)
        self.assertEqual(self.registry.deleted[0], sha(30))
    def test_shared_child_follows_every_candidate_parent(self):
        child = sha(9)
        graph = {sha(1): cleanup.Manifest(sha(1), (child,), {}, True),
            sha(2): cleanup.Manifest(sha(2), (child,), {}, True), child: cleanup.Manifest(child, (), {}, False)}
        order = cleanup.deletion_order(graph)
        self.assertGreater(order.index(child), order.index(sha(1)))
        self.assertGreater(order.index(child), order.index(sha(2)))
    def test_known_historical_patch_indexes_and_shared_children_are_protected(self):
        protected = set()
        for number, tag in enumerate(('8.3.1', '8.4.4', '8.5.1', '8.6.1'), start=3):
            root, children = self.old_index(tag, number * 10)
            protected.update({root, *children})
        self.registry.add_index(sha(70), {'linux/amd64': sha(31), 'linux/arm64': sha(72)})
        result = self.run_cleanup(known_digests=set(self.registry.nodes))
        self.assertEqual(result['tags'], 0)
        self.assertEqual(set(self.registry.deleted), {sha(70), sha(72)})
        self.assertTrue(protected.issubset(self.registry.nodes))
        self.assertEqual(self.hub.deleted, [])
    def test_known_historical_orphan_without_previous_inventory_is_reclaimed(self):
        self.registry.add_index(sha(30), {'linux/amd64': sha(31), 'linux/arm64': sha(32)})
        result = self.run_cleanup(known_digests={sha(30)})
        self.assertEqual(result['manifests'], 3)
        self.assertEqual(set(self.registry.deleted), {sha(30), sha(31), sha(32)})
        self.assertEqual(self.registry.deleted[0], sha(30))
    def test_missing_and_previously_deleted_known_digests_are_idempotent(self):
        self.registry.add_leaf(sha(30))
        known = {sha(30), sha(999)}
        self.assertEqual(self.run_cleanup(known_digests=known)['manifests'], 1)
        self.assertEqual(self.run_cleanup(known_digests=known)['manifests'], 0)
        self.assertEqual(self.registry.deleted, [sha(30)])
    def test_invalid_known_digest_is_rejected_before_planning_or_deletion(self):
        self.old_index()
        self.registry.require_delete = Mock()
        for value in ('latest', 'sha256:invalid', None, 123, IMAGE + '@' + sha(30)):
            with self.subTest(value=value), self.assertRaises(cleanup.CleanupError):
                self.run_cleanup(known_digests=[sha(30), value])
        self.registry.require_delete.assert_not_called()
        self.assertEqual(self.registry.reads, [])
        self.assertEqual(self.registry.deleted, [])
        self.assertEqual(self.hub.deleted, [])
    def test_registry_403_preserves_index_and_its_descendants(self):
        old, _ = self.old_index()
        self.registry.blocked.add(old)
        result = self.run_cleanup()
        self.assertEqual(self.registry.deleted, [old])
        self.assertEqual(result['retained'], 3)
    def test_dry_run_never_deletes(self):
        self.old_index()
        self.run_cleanup(delete=False)
        self.assertEqual(self.hub.deleted, [])
        self.assertEqual(self.registry.deleted, [])
        self.assertTrue(any('Dry run' in line for line in self.messages))
    def test_advanced_publication_aborts_before_any_deletion(self):
        self.old_index()
        self.registry.tags['8.4'] = sha(30)
        with self.assertRaisesRegex(cleanup.CleanupError, 'no longer match'):
            self.run_cleanup()
        self.assertEqual(self.hub.deleted, [])
        self.assertEqual(self.registry.deleted, [])
    def test_publication_changed_after_tags_stops_manifest_deletion(self):
        old, _ = self.old_index()
        self.hub.after_delete = lambda: self.registry.tags.update({'8.4': old})
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup()
        self.assertEqual(self.registry.deleted, [])
    def test_deleted_tag_lingering_in_hub_listing_does_not_block_manifest_gc(self):
        old, children = self.old_index()
        stale_tags = set(self.registry.tags)
        self.hub.list_tags = Mock(side_effect=[stale_tags, stale_tags])
        result = self.run_cleanup()
        self.assertEqual(result['manifests'], 3)
        self.assertEqual(set(self.registry.deleted), {old, *children})
    def test_tags_added_during_deletion_protect_old_index_and_children(self):
        for targets, expected_kept in (((sha(30),), {sha(30), sha(31), sha(32)}),
            ((sha(31),), {sha(31)}), ((sha(30), sha(31)), {sha(30), sha(31), sha(32)})):
            with self.subTest(targets=targets):
                self.setUp()
                old, children = self.old_index()
                def publish_future_tags():
                    for index, target in enumerate(targets):
                        self.registry.tags[f'build-101-{index + 1}-8.4-amd64'] = target
                self.hub.after_delete = publish_future_tags
                result = self.run_cleanup()
                self.assertTrue(expected_kept.isdisjoint(self.registry.deleted))
                self.assertEqual(set(self.registry.deleted), {old, *children} - expected_kept)
                self.assertEqual(result['retained'], len(expected_kept))
                self.assertTrue(expected_kept.issubset(self.registry.nodes))
    def test_unpublished_inventory_cannot_bypass_cli_validation(self):
        self.old_index()
        self.versions['releases']['8.4']['published'] = False
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup()
        self.assertEqual(self.hub.deleted, [])
        self.assertEqual(self.registry.deleted, [])
    def test_permission_failure_precedes_any_delete(self):
        self.old_index()
        self.registry.permission = False
        with self.assertRaisesRegex(cleanup.CleanupError, 'Delete permission'):
            self.run_cleanup()
        self.assertEqual(self.hub.deleted, [])
    def test_invalid_cli_inventory_is_rejected_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'versions.json'
            value = deepcopy(self.versions)
            value['releases']['8.4']['published'] = False
            path.write_text(json.dumps(value), encoding='utf-8')
            with patch.object(cleanup, 'Http') as http, patch('sys.stderr', new_callable=StringIO):
                with self.assertRaises(SystemExit):
                    cleanup.main(['--image', IMAGE, '--versions', str(path), '--delete'])
                http.assert_not_called()

    def test_invalid_known_inventory_cli_is_rejected_before_authentication(self):
        valid = {'schema_version': 1, 'image': IMAGE, 'digests': [sha(30)]}
        cases = [None, [], {**valid, 'schema_version': 2}, {**valid, 'image': 'docker.io/other/image'},
            {**valid, 'digests': None}, {**valid, 'digests': sha(30)},
            {**valid, 'digests': ['latest']}, {**valid, 'digests': [None]}]
        with tempfile.TemporaryDirectory() as directory:
            versions = Path(directory) / 'versions.json'
            versions.write_text(json.dumps(self.versions), encoding='utf-8')
            known = Path(directory) / 'known.json'
            for value in cases:
                with self.subTest(value=value):
                    known.write_text(json.dumps(value), encoding='utf-8')
                    with patch.object(cleanup, 'Http') as http, patch('sys.stderr', new_callable=StringIO):
                        with self.assertRaises(SystemExit):
                            cleanup.main(['--image', IMAGE, '--versions', str(versions),
                                '--known-digests', str(known), '--delete'])
                        http.assert_not_called()
            known.write_text('{invalid json', encoding='utf-8')
            with patch.object(cleanup, 'Http') as http, patch('sys.stderr', new_callable=StringIO):
                with self.assertRaises(SystemExit):
                    cleanup.main(['--image', IMAGE, '--versions', str(versions), '--known-digests', str(known), '--delete'])
                http.assert_not_called()

    def test_known_inventory_is_repository_bound_and_deduplicates_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'known.json'
            path.write_text(json.dumps({'schema_version': 1, 'image': IMAGE,
                'digests': [sha(30), sha(31), sha(30)]}), encoding='utf-8')
            self.assertEqual(cleanup.read_known_digests(path, IMAGE), {sha(30), sha(31)})


class Reply:
    def __init__(self, value=None, status=200, headers=None):
        self.status, self.headers = status, headers or {}
        self.body = json.dumps(value).encode() if value is not None else b''
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class ApiTests(unittest.TestCase):
    def hub(self, replies):
        opener = Mock()
        opener.open.side_effect = [Reply({'access_token': 'hub-jwt'}), *replies]
        http = cleanup.Http(opener=opener, sleep=lambda _: None)
        return cleanup.Hub(IMAGE, 'surfaceocean', SECRET, http), opener
    def test_authentication_and_all_pages_precede_deletion(self):
        next_page = cleanup.HUB + '/v2/namespaces/surfaceocean/repositories/nginx-php-fpm/tags?page=2'
        hub, opener = self.hub([Reply({'results': [{'name': '8.4'}], 'next': next_page}),
            Reply({'results': [{'name': '8.4.4'}], 'next': None}), Reply(status=204)])
        self.assertEqual(hub.list_tags(), {'8.4', '8.4.4'})
        self.assertEqual(opener.open.call_count, 3)
        self.assertTrue(hub.delete_tag('8.4.4'))
        calls = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(json.loads(calls[0].data), {'identifier': 'surfaceocean', 'secret': SECRET})
        self.assertEqual(calls[-1].full_url, cleanup.HUB + '/v2/repositories/surfaceocean/nginx-php-fpm/tags/8.4.4/')
        self.assertEqual(calls[-1].method, 'DELETE')
        self.assertEqual(calls[-1].headers['Authorization'], 'Bearer hub-jwt')
    def test_pagination_cannot_leak_credentials_to_other_hosts_or_repositories(self):
        for url in ('https://evil.example/tags', 'http://hub.docker.com/v2/repositories/surfaceocean/nginx-php-fpm/tags/',
            'https://hub.docker.com/v2/repositories/other/image/tags/', 'https://hub.docker.com@evil.example/tags'):
            with self.subTest(url=url):
                hub, opener = self.hub([Reply({'results': [], 'next': url})])
                with self.assertRaises(cleanup.CleanupError):
                    hub.list_tags()
                self.assertEqual(opener.open.call_count, 2)
    def test_looping_pagination_fails_closed(self):
        first = cleanup.HUB + '/v2/namespaces/surfaceocean/repositories/nginx-php-fpm/tags?page_size=100&page=1'
        hub, _ = self.hub([Reply({'results': [], 'next': first})])
        with self.assertRaisesRegex(cleanup.CleanupError, 'Looping'):
            hub.list_tags()
    def test_registry_auth_requests_delete_scope_and_checks_granted_permission(self):
        for actions, allowed in ((('pull', 'push', 'delete'), True), (('pull', 'push'), False)):
            opener = Mock()
            opener.open.return_value = Reply({'token': jwt(actions)})
            registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
            request = opener.open.call_args.args[0]
            self.assertEqual(parse_qs(urlsplit(request.full_url).query)['scope'], ['repository:surfaceocean/nginx-php-fpm:pull,push,delete'])
            self.assertEqual(base64.b64decode(request.headers['Authorization'].split()[1]).decode(), 'surfaceocean:' + SECRET)
            if allowed:
                registry.require_delete()
            else:
                with self.assertRaisesRegex(cleanup.CleanupError, 'Read, Write, Delete'):
                    registry.require_delete()
    def test_retry_is_bounded_and_honors_retry_after(self):
        opener, sleep = Mock(), Mock()
        opener.open.side_effect = [HTTPError(cleanup.HUB, 429, SECRET, {'Retry-After': '2'}, BytesIO()),
            URLError(SECRET), Reply({'ok': True})]
        result = cleanup.Http(opener=opener, sleep=sleep).request('GET', cleanup.HUB)
        self.assertEqual(result.json(), {'ok': True})
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [2, 2])
        opener.open.side_effect = [HTTPError(cleanup.HUB, 503, SECRET, {}, BytesIO())] * 3
        with self.assertRaisesRegex(cleanup.CleanupError, '3 attempts'):
            cleanup.Http(opener=opener, sleep=lambda _: None).request('GET', cleanup.HUB)
    def test_errors_do_not_expose_credentials_or_response_bodies(self):
        for code in (401, 403, 302):
            opener = Mock()
            opener.open.side_effect = HTTPError(cleanup.HUB, code, SECRET, {'Location': 'https://evil.example'}, BytesIO(SECRET.encode()))
            with self.assertRaises(cleanup.CleanupError) as caught:
                cleanup.Http(opener=opener).request('DELETE', cleanup.HUB, {'Authorization': SECRET})
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(cleanup.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example'))
        with self.assertRaises(cleanup.CleanupError) as caught:
            cleanup.Response(200, {}, SECRET.encode()).json()
        self.assertNotIn(SECRET, str(caught.exception))
    def test_missing_tag_delete_is_idempotent(self):
        hub, _ = self.hub([HTTPError(cleanup.HUB, 404, SECRET, {}, BytesIO())])
        self.assertFalse(hub.delete_tag('8.4.4'))
    def test_manifest_format_and_digest_are_checked(self):
        opener = Mock()
        opener.open.side_effect = [Reply({'token': jwt()}), Reply({'schemaVersion': 2}, headers={'Docker-Content-Digest': sha(1)})]
        registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
        with self.assertRaisesRegex(cleanup.CleanupError, 'different manifest digest'):
            registry.manifest(sha(2))

    def test_multiple_unknown_attestations_preserve_every_index_child(self):
        descriptors = [{'digest': sha(number), 'platform': {'os': os, 'architecture': arch}}
            for number, os, arch in ((1, 'linux', 'amd64'), (2, 'linux', 'arm64'),
                (3, 'unknown', 'unknown'), (4, 'unknown', 'unknown'))]
        opener = Mock()
        opener.open.side_effect = [Reply({'token': jwt()}),
            Reply({'schemaVersion': 2, 'manifests': descriptors}, headers={'Docker-Content-Digest': sha(10)})]
        registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
        manifest = registry.manifest(sha(10))
        self.assertEqual(set(manifest.children), {sha(1), sha(2), sha(3), sha(4)})
        self.assertEqual(manifest.platforms, {'linux/amd64': sha(1), 'linux/arm64': sha(2), 'unknown/unknown': None})
        self.assertTrue(manifest.is_index)

    def test_expired_registry_token_is_refreshed_once_for_manifest_read(self):
        old, new = jwt() + '.old', jwt() + '.new'
        opener = Mock()
        opener.open.side_effect = [Reply({'token': old}),
            HTTPError(cleanup.REGISTRY, 401, SECRET, {}, BytesIO()), Reply({'token': new}),
            Reply({'schemaVersion': 2}, headers={'Docker-Content-Digest': sha(30)})]
        registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
        self.assertEqual(registry.manifest(sha(30)).digest, sha(30))
        calls = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1].headers['Authorization'], 'Bearer ' + old)
        self.assertEqual(calls[3].headers['Authorization'], 'Bearer ' + new)
        self.assertEqual(calls[1].full_url, calls[3].full_url)

    def test_delete_rechecks_permission_after_registry_token_refresh(self):
        for granted in (True, False):
            with self.subTest(granted=granted):
                opener = Mock()
                replies = [Reply({'token': jwt()}), HTTPError(cleanup.REGISTRY, 401, SECRET, {}, BytesIO()),
                    Reply({'token': jwt(('pull', 'push', 'delete') if granted else ('pull', 'push'))})]
                if granted:
                    replies.append(Reply(status=202))
                opener.open.side_effect = replies
                registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
                if granted:
                    self.assertEqual(registry.delete_manifest(sha(30)), 202)
                else:
                    with self.assertRaisesRegex(cleanup.CleanupError, 'Read, Write, Delete'):
                        registry.delete_manifest(sha(30))
                methods = [call.args[0].method for call in opener.open.call_args_list]
                self.assertEqual(methods, ['GET', 'DELETE', 'GET'] + (['DELETE'] if granted else []))

    def test_repeated_registry_unauthorized_fails_without_further_refresh(self):
        for method in ('GET', 'DELETE'):
            with self.subTest(method=method):
                opener = Mock()
                opener.open.side_effect = [Reply({'token': jwt()}),
                    HTTPError(cleanup.REGISTRY, 401, SECRET, {}, BytesIO()), Reply({'token': jwt()}),
                    HTTPError(cleanup.REGISTRY, 401, SECRET, {}, BytesIO())]
                registry = cleanup.Registry(IMAGE, 'surfaceocean', SECRET, cleanup.Http(opener=opener))
                with self.assertRaises(cleanup.CleanupError) as caught:
                    if method == 'GET':
                        registry.manifest(sha(30))
                    else:
                        registry.delete_manifest(sha(30))
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(opener.open.call_count, 4)


if __name__ == '__main__':
    unittest.main()

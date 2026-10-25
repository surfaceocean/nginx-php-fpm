"""Exercise historical digest discovery without network access or a temporary repo."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('dockerhub_history', ROOT / 'scripts/collect-dockerhub-digests.py')
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)
IMAGE = 'docker.io/surfaceocean/nginx-php-fpm'
SECRET = 'must-not-leak-git-error-or-inventory-content'


def sha(number):
    return 'sha256:' + f'{number:064x}'


def oid(number):
    return f'{number:040x}'


def inventory(number=10):
    return {'schema_version': 1, 'image': IMAGE, 'releases': {
        '8.4': {'published': True, 'digest': sha(number), 'platforms': {
            'linux/amd64': {'digest': sha(number + 1)},
            'linux/arm64': {'digest': sha(number + 2)}}}}}


class FakeGit:
    def __init__(self, files=None, commits=None, shallow='false'):
        self.files = files or {}
        self.commits = list(self.files) if commits is None else commits
        self.shallow, self.calls = shallow, []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        arguments = command[1:]
        if arguments == ['rev-parse', '--is-shallow-repository']:
            output = self.shallow + '\n'
        elif arguments == ['log', '--format=%H', '--full-history', '-m',
                '--diff-filter=AM', 'HEAD', '--', 'versions.json']:
            output = '\n'.join(self.commits) + ('\n' if self.commits else '')
        elif arguments[0] == 'rev-parse':
            commit = arguments[1].removesuffix(':versions.json')
            output = self.files[commit][0] + '\n'
        elif arguments[:2] == ['cat-file', 'blob']:
            source = next(value for blob, value in self.files.values() if blob == arguments[2])
            output = source if isinstance(source, str) else json.dumps(source)
        else:
            raise AssertionError('Unexpected Git command')
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')


class HistoryTests(unittest.TestCase):
    def test_all_historical_roots_and_both_platforms_are_collected(self):
        git = FakeGit({oid(1): (oid(101), inventory()), oid(2): (oid(102), inventory(20))})
        self.assertEqual(history.collect_digests(IMAGE, git), [sha(n) for n in (10, 11, 12, 20, 21, 22)])
        self.assertTrue(all(call[1]['shell'] is False for call in git.calls))
        self.assertIn('HEAD', git.calls[1][0])

    def test_duplicate_merge_commits_and_identical_blobs_are_read_once(self):
        git = FakeGit({oid(1): (oid(101), inventory()), oid(2): (oid(101), inventory())},
            [oid(1), oid(1), oid(2)])
        self.assertEqual(history.collect_digests(IMAGE, git), [sha(n) for n in (10, 11, 12)])
        self.assertEqual(sum(command[1:3] == ['cat-file', 'blob'] for command, _ in git.calls), 1)
        self.assertEqual(sum(command == ['git', 'rev-parse', oid(1) + ':versions.json']
            for command, _ in git.calls), 1)

    def test_foreign_image_and_initial_unpublished_inventories_are_skipped(self):
        foreign = {'image': 'docker.io/another/repository', 'schema_version': 'older'}
        unpublished = {'schema_version': 1, 'image': IMAGE,
            'releases': {'8.4': {'published': False, 'digest': None}}}
        git = FakeGit({oid(1): (oid(101), foreign), oid(2): (oid(102), unpublished)})
        self.assertEqual(history.collect_digests(IMAGE, git), [])

    def test_empty_history_is_supported(self):
        self.assertEqual(history.collect_digests(IMAGE, FakeGit()), [])

    def test_matching_published_inventory_fails_closed_if_malformed(self):
        mutations = [lambda x: x.update(schema_version=2), lambda x: x.update(releases=[]),
            lambda x: x['releases'].update({'8.4': None}),
            lambda x: x['releases']['8.4'].update(digest=None),
            lambda x: x['releases']['8.4'].update(platforms={}),
            lambda x: x['releases']['8.4']['platforms'].update({'linux/arm64': None}),
            lambda x: x['releases']['8.4']['platforms']['linux/arm64'].update(digest='latest')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = inventory()
                mutate(value)
                with self.assertRaises(history.HistoryError):
                    history.collect_digests(IMAGE, FakeGit({oid(1): (oid(101), value)}))

    def test_non_object_and_invalid_json_fail_without_echoing_input(self):
        for source in ('{' + SECRET, json.dumps([SECRET])):
            with self.subTest(source=source), self.assertRaises(history.HistoryError) as caught:
                history.collect_digests(IMAGE, FakeGit({oid(1): (oid(101), source)}))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_shallow_checkout_fails_before_reading_inventory(self):
        git = FakeGit(shallow='true')
        with self.assertRaisesRegex(history.HistoryError, 'fetch-depth: 0'):
            history.collect_digests(IMAGE, git)
        self.assertEqual(len(git.calls), 1)

    def test_unexpected_shallow_result_and_unsafe_object_ids_are_rejected(self):
        for git in (FakeGit(shallow='unknown'), FakeGit(commits=['HEAD:other-file']),
                FakeGit({oid(1): ('--help', inventory())})):
            with self.subTest(git=git), self.assertRaises(history.HistoryError):
                history.collect_digests(IMAGE, git)

    def test_git_command_errors_are_sanitized(self):
        failures = [Mock(return_value=subprocess.CompletedProcess([], 128, '', SECRET)),
            Mock(side_effect=OSError(SECRET)), Mock(side_effect=subprocess.SubprocessError(SECRET))]
        for run in failures:
            with self.subTest(run=run), self.assertRaises(history.HistoryError) as caught:
                history.collect_digests(IMAGE, run)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_git_blob_read_failure_does_not_return_a_partial_inventory(self):
        git = FakeGit({oid(1): (oid(101), inventory()), oid(2): (oid(102), inventory(20))})
        def run(command, **kwargs):
            if command == ['git', 'cat-file', 'blob', oid(102)]:
                return subprocess.CompletedProcess(command, 128, '', SECRET)
            return git(command, **kwargs)
        with self.assertRaises(history.HistoryError):
            history.collect_digests(IMAGE, run)

    def test_invalid_image_is_rejected_before_git(self):
        run = Mock()
        for image in ('surfaceocean/nginx-php-fpm', IMAGE + ':8.4', IMAGE + ';command'):
            with self.subTest(image=image), self.assertRaises(history.HistoryError):
                history.collect_digests(image, run)
        run.assert_not_called()

    def test_json_file_output_contains_sorted_deduplicated_digests(self):
        git = FakeGit({oid(1): (oid(101), inventory(20)), oid(2): (oid(102), inventory())})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'historical-digests.json'
            value = history.write_inventory(IMAGE, output, git)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), value)
            self.assertEqual(value, {'schema_version': 1, 'image': IMAGE,
                'digests': [sha(n) for n in (10, 11, 12, 20, 21, 22)]})

    def test_output_errors_and_cli_errors_are_sanitized(self):
        with patch.object(history, 'collect_digests', return_value=[]), \
                patch.object(Path, 'write_text', side_effect=PermissionError(SECRET)):
            with self.assertRaises(history.HistoryError) as caught:
                history.write_inventory(IMAGE, 'unused.json')
            self.assertNotIn(SECRET, str(caught.exception))
        error, output = StringIO(), StringIO()
        with patch.object(history, 'write_inventory', side_effect=history.HistoryError('Read failed')), \
                redirect_stderr(error), redirect_stdout(output):
            status = history.main(['--image', IMAGE, '--output', 'unused.json'])
        self.assertEqual(status, 1)
        self.assertEqual(error.getvalue(), 'ERROR: Read failed\n')
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()

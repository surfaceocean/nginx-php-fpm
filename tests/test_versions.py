"""Exercise inventory validation and publishing without a Docker daemon."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("versions", ROOT / "scripts/update-versions.py")
versions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(versions)
CONFIGURED_SERIES = versions.PHP_SERIES


def runtime(series="8.5", patch_number=1):
    result = {
        "php": f"{series}.{patch_number}",
        "extensions": {name: "1.2.3" for name in versions.REQUIRED_EXTENSIONS},
        "tools": {name: "1.2.3" for name in ("nginx", "composer", "node", "npm")},
        "libraries": {"icu": "76.1"},
        "native_packages": {"libpq-dev": "17.6-0+deb13u1"},
    }
    # PHP reports PDO with its original case, unlike most extension names.
    result["extensions"]["PDO"] = result["extensions"].pop("pdo")
    return result


def matrix():
    return {
        (series, platform): versions.make_record(
            runtime(series), series, platform, versions.IMAGE, f"sha256:{index:064x}"
        )
        for index, (series, platform) in enumerate(
            ((s, p) for s in versions.PHP_SERIES for p in versions.PLATFORMS), start=1
        )
    }


class InventoryTests(unittest.TestCase):
    def setUp(self):
        # These fixed scenarios are independent of future edits to the supported list.
        configured = patch.object(versions, "PHP_SERIES", ("8.4", "8.5"))
        configured.start()
        self.addCleanup(configured.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def save_records(self, records):
        for index, record in enumerate(records.values()):
            versions.write_json(self.directory / f"{index}.json", record)

    def test_complete_matrix_preserves_observed_versions(self):
        self.save_records(matrix())
        records = versions.collect_records(self.directory, versions.IMAGE)
        result = versions.document(records, versions.IMAGE)
        self.assertEqual(set(result["releases"]), {"8.4", "8.5"})
        observed = result["releases"]["8.5"]["platforms"]["linux/arm64"]
        self.assertEqual(observed["php"], "8.5.1")
        self.assertEqual(observed["extensions"]["redis"], "1.2.3")
        self.assertEqual(observed["native_packages"]["libpq-dev"], "17.6-0+deb13u1")
        self.assertEqual(observed["libraries"]["icu"], "76.1")

    def test_missing_architecture_rejects_release(self):
        records = matrix()
        records.pop(("8.5", "linux/arm64"))
        self.save_records(records)
        with self.assertRaisesRegex(ValueError, "Incomplete build matrix"):
            versions.collect_records(self.directory, versions.IMAGE)

    def test_duplicate_architecture_rejects_release(self):
        records = matrix()
        self.save_records(records)
        versions.write_json(self.directory / "duplicate.json", next(iter(records.values())))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            versions.collect_records(self.directory, versions.IMAGE)

    def test_php_patch_must_match_across_architectures(self):
        records = matrix()
        records[("8.5", "linux/arm64")]["runtime"]["php"] = "8.5.2"
        self.save_records(records)
        with self.assertRaisesRegex(ValueError, "patch versions differ"):
            versions.collect_records(self.directory, versions.IMAGE)

    def test_different_registry_image_rejected(self):
        records = matrix()
        records[("8.5", "linux/arm64")]["image"] = "docker.io/other/image"
        self.save_records(records)
        with self.assertRaisesRegex(ValueError, "image name differs"):
            versions.collect_records(self.directory, versions.IMAGE)

    def test_missing_required_extension_rejected(self):
        value = runtime()
        del value["extensions"]["imagick"]
        with self.assertRaisesRegex(ValueError, "imagick"):
            versions.check_runtime(value, "8.5")

    def test_required_extension_needs_actual_version(self):
        for invalid in (None, "", " ", 123):
            with self.subTest(value=invalid):
                value = runtime()
                value["extensions"]["redis"] = invalid
                with self.assertRaises(ValueError):
                    versions.check_runtime(value, "8.5")

    def test_missing_tool_version_rejected(self):
        value = runtime()
        value["tools"]["composer"] = ""
        with self.assertRaisesRegex(ValueError, "Composer"):
            versions.check_runtime(value, "8.5")

    def test_legacy_and_prerelease_php_rejected(self):
        for php, series in (("8.3.30", "8.3"), ("8.5.0RC1", "8.5"), ("8.4.1", "8.5")):
            with self.subTest(php=php), self.assertRaises(ValueError):
                value = runtime()
                value["php"] = php
                versions.check_runtime(value, series)

    def test_invalid_digests_rejected(self):
        for digest in ("latest", "sha256:abc", "sha256:" + "z" * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                versions.check_digest(digest)

    def test_all_digests_checked_before_any_publication(self):
        records = matrix()
        records[("8.5", "linux/arm64")]["digest"] = None
        with patch.object(versions.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                versions.document(records, versions.IMAGE, publish=True)
            run.assert_not_called()

    def test_release_uses_tested_digests_and_latest_is_85(self):
        selected = [r for (series, _), r in matrix().items() if series == "8.5"]
        descriptor = {
            "digest": "sha256:" + "a" * 64,
            "manifests": [
                {"platform": {"os": "linux", "architecture": r["platform"].split("/")[1]},
                 "digest": r["digest"]}
                for r in selected
            ],
        }
        with patch.object(versions.subprocess, "run") as run, patch.object(
            versions.subprocess, "check_output", return_value=json.dumps(descriptor)
        ):
            tags, digest = versions.publish_release(versions.IMAGE, "8.5", selected)
        self.assertIn(versions.IMAGE + ":latest", tags)
        self.assertIn(versions.IMAGE + ":8.5.1", tags)
        for record in selected:
            self.assertIn(versions.IMAGE + "@" + record["digest"], run.call_args.args[0])
        self.assertEqual(digest, descriptor["digest"])

    def test_matrix_and_latest_follow_replacement_and_single_version_configurations(self):
        for supported in (("8.5", "8.6"), ("8.9", "8.10"), ("8.6",)):
            with self.subTest(supported=supported), patch.object(versions, "PHP_SERIES", supported):
                records = matrix()
                result = versions.document(records, versions.IMAGE)
                self.assertEqual(set(result["releases"]), set(supported))
                self.assertEqual(len(records), len(supported) * len(versions.PLATFORMS))
                with self.assertRaises(ValueError):
                    versions.make_record(runtime("8.4"), "8.4", "linux/amd64", versions.IMAGE)
                for series in supported:
                    selected = [r for (php, _), r in records.items() if php == series]
                    descriptor = {"digest": "sha256:" + "a" * 64, "manifests": [
                        {"platform": {"os": "linux", "architecture": r["platform"].split("/")[1]},
                         "digest": r["digest"]} for r in selected]}
                    with patch.object(versions.subprocess, "run"), patch.object(
                        versions.subprocess, "check_output", return_value=json.dumps(descriptor)
                    ):
                        tags, _ = versions.publish_release(versions.IMAGE, series, selected)
                    self.assertEqual(versions.IMAGE + ":latest" in tags, series == supported[-1])

    def test_registry_manifest_must_match_tested_architectures(self):
        selected = [r for (series, _), r in matrix().items() if series == "8.4"]
        with patch.object(versions.subprocess, "run"), patch.object(
            versions.subprocess, "check_output", return_value='{"manifests": []}'
        ), self.assertRaisesRegex(ValueError, "does not match"):
            versions.publish_release(versions.IMAGE, "8.4", selected)

    def test_local_build_preserves_other_php_and_clears_publication(self):
        output = self.directory / "versions.json"
        previous = versions.document(matrix(), versions.IMAGE)
        previous["releases"]["8.5"].update(published=True, tags=["old:latest"], digest="old")
        versions.write_json(output, previous)
        record = versions.make_record(runtime("8.5", 2), "8.5", "linux/amd64", versions.IMAGE)
        result = versions.local_document(record, versions.IMAGE, output)
        self.assertEqual(result["releases"]["8.4"], previous["releases"]["8.4"])
        release = result["releases"]["8.5"]
        self.assertFalse(release["published"])
        self.assertEqual(release["tags"], [])
        self.assertIsNone(release["digest"])
        self.assertIsNone(release["platforms"]["linux/amd64"]["digest"])
        self.assertEqual(release["platforms"]["linux/amd64"]["php"], "8.5.2")
        self.assertIn("linux/arm64", release["platforms"])

    def test_changing_local_image_resets_inventory(self):
        output = self.directory / "versions.json"
        versions.write_json(output, versions.document(matrix(), versions.IMAGE))
        record = versions.make_record(runtime(), "8.5", "linux/amd64", "local/image")
        result = versions.local_document(record, "local/image", output)
        self.assertEqual(set(result["releases"]), {"8.5"})

    def test_invalid_input_cannot_replace_existing_versions(self):
        output = self.directory / "versions.json"
        original = '{"previous": "successful build"}\n'
        output.write_text(original, encoding="utf-8")
        manifest = self.directory / "runtime.json"
        series = CONFIGURED_SERIES[-1]
        value = runtime(series)
        del value["extensions"]["pdo_pgsql"]
        versions.write_json(manifest, value)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/update-versions.py"), "--local-manifest",
             str(manifest), "--php", series, "--platform", "linux/amd64", "--output", str(output)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pdo_pgsql", result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

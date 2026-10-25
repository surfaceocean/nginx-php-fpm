"""Exercise one-file PHP release changes without Docker or network access."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("php_release_config", ROOT / "scripts/php_versions.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "php-versions.json"

    def save(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_numeric_order_controls_highest_regardless_of_file_order(self):
        self.save(["8.10", "8.5", "8.9"])
        supported = config.load_versions(self.path)
        self.assertEqual(supported, ("8.5", "8.9", "8.10"))
        self.assertEqual(config.resolve_version("latest", supported), "8.10")
        self.assertEqual(config.resolve_version("8.9", supported), "8.9")

    def test_single_release_is_valid_and_is_latest(self):
        self.save(["8.6"])
        self.assertEqual(config.load_versions(self.path), ("8.6",))
        self.assertEqual(config.resolve_version("latest", config.load_versions(self.path)), "8.6")

    def test_empty_duplicate_and_malformed_lists_are_rejected(self):
        for value in ([], {}, None, "8.5", [8.5], [True], [None], ["8.5", "8.5"],
                      ["8.05"], ["08.5"], ["0.5"], ["8.5.1"], ["8.6-rc1"],
                      [" 8.5"], ["8.5\n"], ["8/5"], ["latest"]):
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaises(ValueError):
                    config.load_versions(self.path)

    def test_missing_or_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            config.load_versions(self.path)
        self.path.write_text('["8.5",', encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load_versions(self.path)

    def test_removed_or_unconfigured_series_cannot_be_resolved(self):
        self.save(["8.5", "8.6"])
        for value in ("8.4", "8.6.1", "8.7", "latest "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.resolve_version(value, config.load_versions(self.path))

    def test_cli_outputs_list_matrix_and_default_from_explicit_config(self):
        self.save(["8.10", "8.9"])
        for option, expected in (("--list", "8.9\n8.10"), ("--json", '["8.9","8.10"]'),
                                 ("--latest", "8.10")):
            with self.subTest(option=option):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/php_versions.py"),
                    "--config", str(self.path), option], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if option == "--json":
                    self.assertEqual(json.loads(result.stdout), json.loads(expected))
                else:
                    self.assertEqual(result.stdout.strip(), expected)


class OneFileChangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.scripts = self.directory / "scripts"
        self.scripts.mkdir()
        for name in ("php_versions.py", "update-versions.py"):
            shutil.copyfile(ROOT / "scripts" / name, self.scripts / name)
        self.path = self.directory / "php-versions.json"
        self.set_config(["8.4", "8.5"])

    def set_config(self, versions):
        self.path.write_text(json.dumps(versions), encoding="utf-8")

    def cli(self, script, *arguments):
        return subprocess.run([sys.executable, str(self.scripts / script), *arguments],
            cwd=self.directory, capture_output=True, text=True)

    def test_only_editing_config_changes_cli_and_complete_inventory_matrix(self):
        self.assertEqual(self.cli("php_versions.py", "--latest").stdout.strip(), "8.5")
        self.set_config(["8.6", "8.5"])
        supported = self.cli("php_versions.py", "--json")
        self.assertEqual(supported.returncode, 0, supported.stderr)
        self.assertEqual(json.loads(supported.stdout), ["8.5", "8.6"])
        self.assertEqual(self.cli("php_versions.py", "--check", "latest").stdout.strip(), "8.6")
        self.assertNotEqual(self.cli("php_versions.py", "--check", "8.4").returncode, 0)
        # Reuse the inventory runtime fixture; only the copied config selects releases.
        from test_versions import runtime, versions
        records = self.directory / "records"
        records.mkdir()
        for index, (series, platform) in enumerate(
            ((series, platform) for series in ("8.5", "8.6") for platform in versions.PLATFORMS), 1
        ):
            value = {"php_series": series, "platform": platform, "image": versions.IMAGE,
                "digest": "sha256:" + f"{index:064x}", "runtime": runtime(series)}
            (records / f"{index}.json").write_text(json.dumps(value), encoding="utf-8")
        output = self.directory / "versions.json"
        result = self.cli("update-versions.py", "--records", str(records), "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(set(value["releases"]), {"8.5", "8.6"})
        self.assertEqual(value["releases"]["8.6"]["php"], "8.6.1")
        manifest = self.directory / "runtime.json"
        manifest.write_text(json.dumps(runtime("8.4")), encoding="utf-8")
        result = self.cli("update-versions.py", "--record-manifest", str(manifest),
            "--php", "8.4", "--platform", "linux/amd64", "--output", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), value)

    def test_only_editing_config_also_supports_a_single_release(self):
        self.set_config(["8.6"])
        self.assertEqual(self.cli("php_versions.py", "--latest").stdout.strip(), "8.6")
        self.assertEqual(json.loads(self.cli("php_versions.py", "--json").stdout), ["8.6"])
        from test_versions import runtime
        manifest = self.directory / "runtime.json"
        manifest.write_text(json.dumps(runtime("8.6")), encoding="utf-8")
        output = self.directory / "record.json"
        result = self.cli("update-versions.py", "--record-manifest", str(manifest),
            "--php", "8.6", "--platform", "linux/amd64", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["php_series"], "8.6")

    def test_invalid_configuration_stops_inventory_before_any_docker_call(self):
        self.set_config(["8.5", "8.5"])
        spec = importlib.util.spec_from_file_location("invalid_config_inventory", self.scripts / "update-versions.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(subprocess, "run") as run:
            with self.assertRaises(ValueError):
                spec.loader.exec_module(module)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

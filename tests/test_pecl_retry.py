"""Exercise the Dockerfile's PECL commands without Docker or network access."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ("apcu", "imagick", "memcached", "redis", "rdkafka")
COMMANDS = ["channel-update pecl.php.net", "config-set preferred_state stable"] + [
    f"install {extension}" for extension in EXTENSIONS
]


def pecl_run_script():
    """Read the actual RUN instruction, preserving its shell control flow."""
    lines = (ROOT / "src/docker/Dockerfile").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("RUN "):
            continue
        command = line[4:]
        while command.rstrip().endswith("\\"):
            index += 1
            command = command.rstrip()[:-1] + "\n" + lines[index]
        if "pecl channel-update" in command:
            return command
    raise AssertionError("The Dockerfile has no PECL channel-update RUN instruction")


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "requires POSIX sh")
class PeclRetryTests(unittest.TestCase):
    def run_pecl(self, failing_command="", failures=0):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            scripts = {
                "pecl": """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$PECL_CALLS"
if [ "$1" = install ] && IFS= read -r input; then
    echo 'PECL install must receive EOF on stdin' >&2
    exit 73
fi
if [ "$*" = "$PECL_FAIL_COMMAND" ]; then
    count=0
    if [ -f "$PECL_COUNTER" ]; then
        read -r count < "$PECL_COUNTER"
    fi
    count=$((count + 1))
    printf '%s\\n' "$count" > "$PECL_COUNTER"
    if [ "$count" -le "$PECL_FAILURES" ]; then
        exit 42
    fi
fi
""",
                "sleep": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SLEEP_CALLS"\n',
                "docker-php-ext-enable": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ENABLE_CALLS"\n',
                # Keep cleanup inside the actual RUN instruction harmless.
                "rm": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CLEANUP_CALLS"\n',
            }
            for name, source in scripts.items():
                executable = directory / name
                executable.write_text(source, encoding="utf-8")
                executable.chmod(0o755)
            log_names = ("PECL_CALLS", "SLEEP_CALLS", "ENABLE_CALLS", "CLEANUP_CALLS")
            environment = {
                **os.environ,
                "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
                "PECL_FAIL_COMMAND": failing_command,
                "PECL_FAILURES": str(failures),
                "PECL_COUNTER": str(directory / "counter"),
                **{name: str(directory / name) for name in log_names},
            }
            result = subprocess.run(
                [shutil.which("sh"), "-c", pecl_run_script()],
                env=environment,
                input="unexpected interactive input\n",
                text=True,
                capture_output=True,
                timeout=10,
            )
            logs = {
                name: (directory / name).read_text(encoding="utf-8").splitlines()
                if (directory / name).exists() else []
                for name in log_names
            }
            return result, logs

    def assert_enabled(self, logs):
        self.assertEqual(logs["ENABLE_CALLS"], [" ".join(EXTENSIONS)])

    def test_success_runs_commands_once_without_waiting(self):
        result, logs = self.run_pecl()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(logs["PECL_CALLS"], COMMANDS)
        self.assertEqual(logs["SLEEP_CALLS"], [])
        self.assert_enabled(logs)

    def test_channel_update_can_succeed_on_the_third_retry(self):
        result, logs = self.run_pecl(COMMANDS[0], failures=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(logs["PECL_CALLS"], [COMMANDS[0]] * 4 + COMMANDS[1:])
        self.assertEqual(logs["SLEEP_CALLS"], ["10", "20", "30"])
        self.assert_enabled(logs)

    def test_channel_update_failure_stops_after_three_retries(self):
        result, logs = self.run_pecl(COMMANDS[0], failures=99)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(logs["PECL_CALLS"], [COMMANDS[0]] * 4)
        self.assertEqual(logs["SLEEP_CALLS"], ["10", "20", "30"])
        self.assertEqual(logs["ENABLE_CALLS"], [])
        self.assertEqual(logs["CLEANUP_CALLS"], [])

    def test_extension_retries_preserve_previously_installed_extensions(self):
        result, logs = self.run_pecl("install redis", failures=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            logs["PECL_CALLS"], COMMANDS[:5] + ["install redis"] * 4 + COMMANDS[6:]
        )
        self.assertEqual(logs["SLEEP_CALLS"], ["10", "20", "30"])
        self.assert_enabled(logs)

    def test_extension_failure_stops_before_later_install_and_enable(self):
        result, logs = self.run_pecl("install redis", failures=99)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(logs["PECL_CALLS"], COMMANDS[:5] + ["install redis"] * 4)
        self.assertEqual(logs["SLEEP_CALLS"], ["10", "20", "30"])
        self.assertEqual(logs["ENABLE_CALLS"], [])
        self.assertEqual(logs["CLEANUP_CALLS"], [])


if __name__ == "__main__":
    unittest.main()

"""Exercise the Dockerfile's PIE commands without Docker or network access."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ("apcu", "imagick", "memcached", "redis", "rdkafka")
PACKAGES = (
    "apcu/apcu", "imagick/imagick", "php-memcached/php-memcached",
    "phpredis/phpredis", "rdkafka/rdkafka",
)
MAX_RETRIES = 10
RETRY_DELAYS = [str(retry * 10) for retry in range(1, MAX_RETRIES + 1)]
COMMANDS = [
    f"install --no-interaction --no-cache --skip-enable-extension {package}"
    for package in PACKAGES
]


def pie_run_script():
    """Read the actual RUN instruction, preserving its shell control flow."""
    lines = (ROOT / "src/docker/Dockerfile").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("RUN "):
            continue
        command = line[4:]
        while command.rstrip().endswith("\\"):
            index += 1
            command = command.rstrip()[:-1] + "\n" + lines[index]
        if "retry pie install" in command:
            return command
    raise AssertionError("The Dockerfile has no PIE install RUN instruction")


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "requires POSIX sh")
class PieRetryTests(unittest.TestCase):
    def run_pie(self, failing_command="", failures=0):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            scripts = {
                "pie": """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$PIE_CALLS"
if [ "$#" -ne 5 ] || [ "$1" != install ] || [ "$2" != --no-interaction ] \\
    || [ "$3" != --no-cache ] || [ "$4" != --skip-enable-extension ]; then
    echo 'Unexpected PIE install arguments' >&2
    exit 74
fi
if IFS= read -r input; then
    echo 'PIE install must receive EOF on stdin' >&2
    exit 73
fi
if [ "$*" = "$PIE_FAIL_COMMAND" ]; then
    count=0
    if [ -f "$PIE_COUNTER" ]; then
        read -r count < "$PIE_COUNTER"
    fi
    count=$((count + 1))
    printf '%s\\n' "$count" > "$PIE_COUNTER"
    if [ "$count" -le "$PIE_FAILURES" ]; then
        exit 42
    fi
fi
""",
                "sleep": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SLEEP_CALLS"\n',
                "docker-php-ext-enable": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ENABLE_CALLS"\n',
            }
            for name, source in scripts.items():
                executable = directory / name
                executable.write_text(source, encoding="utf-8")
                executable.chmod(0o755)
            log_names = ("PIE_CALLS", "SLEEP_CALLS", "ENABLE_CALLS")
            environment = {
                **os.environ,
                "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
                "PIE_FAIL_COMMAND": failing_command,
                "PIE_FAILURES": str(failures),
                "PIE_COUNTER": str(directory / "counter"),
                **{name: str(directory / name) for name in log_names},
            }
            result = subprocess.run(
                [shutil.which("sh"), "-c", pie_run_script()],
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
        result, logs = self.run_pie()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(logs["PIE_CALLS"], COMMANDS)
        self.assertEqual(logs["SLEEP_CALLS"], [])
        self.assert_enabled(logs)

    def test_first_extension_can_succeed_on_the_tenth_retry(self):
        result, logs = self.run_pie(COMMANDS[0], failures=MAX_RETRIES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(logs["PIE_CALLS"], [COMMANDS[0]] * (MAX_RETRIES + 1) + COMMANDS[1:])
        self.assertEqual(logs["SLEEP_CALLS"], RETRY_DELAYS)
        self.assert_enabled(logs)

    def test_first_extension_failure_stops_after_ten_retries(self):
        result, logs = self.run_pie(COMMANDS[0], failures=99)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(logs["PIE_CALLS"], [COMMANDS[0]] * (MAX_RETRIES + 1))
        self.assertEqual(logs["SLEEP_CALLS"], RETRY_DELAYS)
        self.assertEqual(logs["ENABLE_CALLS"], [])

    def test_extension_retries_preserve_previously_installed_extensions(self):
        result, logs = self.run_pie(COMMANDS[3], failures=MAX_RETRIES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            logs["PIE_CALLS"], COMMANDS[:3] + [COMMANDS[3]] * (MAX_RETRIES + 1) + COMMANDS[4:]
        )
        self.assertEqual(logs["SLEEP_CALLS"], RETRY_DELAYS)
        self.assert_enabled(logs)

    def test_extension_failure_stops_before_later_install_and_enable(self):
        result, logs = self.run_pie(COMMANDS[3], failures=99)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(logs["PIE_CALLS"], COMMANDS[:3] + [COMMANDS[3]] * (MAX_RETRIES + 1))
        self.assertEqual(logs["SLEEP_CALLS"], RETRY_DELAYS)
        self.assertEqual(logs["ENABLE_CALLS"], [])


if __name__ == "__main__":
    unittest.main()

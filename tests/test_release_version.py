"""Exercise the release workflow's version step without contacting GitHub."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from support import ROOT, make_plugin


class ReleaseVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = (ROOT / ".github/workflows/release-on-pr-merge.yml").read_text(
            encoding="utf-8"
        )
        step = workflow.split("        id: read_version\n", 1)[1].split(
            "\n      - name:", 1
        )[0]
        cls.script = textwrap.dedent(step.split("        run: |\n", 1)[1])

    def run_read_version(self, metadata, main='self.plugin_version = "v0.0.1"'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if metadata is not None:
                (root / "metadata.yaml").write_text(metadata, encoding="utf-8")
            if main is not None:
                (root / "main.py").write_text(main, encoding="utf-8")
            environment = dict(os.environ, GITHUB_OUTPUT="github_output.txt")
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", self.script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            output_path = root / "github_output.txt"
            output = (
                output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            )
        return result, output

    def test_current_release_matches_runtime_metadata(self):
        result, output = self.run_read_version(
            (ROOT / "metadata.yaml").read_text(encoding="utf-8"),
            (ROOT / "main.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        version = make_plugin().plugin_version.removeprefix("v")
        self.assertIn(f"current_version={version}\n", output)
        self.assertIn("today=", output)

    def test_metadata_takes_precedence_over_legacy_registration(self):
        result, output = self.run_read_version(
            "version: v2.8.11\n", main='version="v0.0.1"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("current_version=2.8.11\n", output)

    def test_common_yaml_version_formats(self):
        for value in (
            "v2.8.11",
            "2.8.11",
            '"v2.8.11"',
            "'2.8.11'",
            "  v2.8.11  ",
            '"v2.8.11" # release version',
            "v2.8.11\r",
        ):
            with self.subTest(value=value):
                result, output = self.run_read_version(f"version: {value}\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("current_version=2.8.11\n", output)

    def test_invalid_versions_do_not_produce_outputs(self):
        for metadata in (
            "",
            "name: daily_limit\n",
            "version: \n",
            "version: v\n",
            "version: v2..11\n",
            "version: v2.8\n",
            "version: v2.8.11.0\n",
            "version: latest\n",
            "version: \"v2.8.11'\n",
            "version: v2.8.11\nversion: v9.8.7\n",
        ):
            with self.subTest(metadata=metadata):
                result, output = self.run_read_version(metadata)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("metadata.yaml must declare a version", result.stderr)
                self.assertEqual(output, "")

    def test_missing_metadata_does_not_fall_back_to_main(self):
        result, output = self.run_read_version(None, main='version="v0.0.1"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("metadata.yaml is required", result.stderr)
        self.assertEqual(output, "")

    def test_version_does_not_require_main(self):
        result, output = self.run_read_version("version: v2.8.11\n", main=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("current_version=2.8.11\n", output)

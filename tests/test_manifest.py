import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = {
    "linux": ROOT / "herdr-plugin.toml",
    "windows": ROOT / "windows" / "herdr-plugin.toml",
}


class ManifestTests(unittest.TestCase):
    def test_release_identity_and_runtime_contract(self):
        for platform, manifest in MANIFESTS.items():
            with self.subTest(platform=platform):
                text = manifest.read_text(encoding="utf-8")
                self.assertIn('id = "yigitkg.local-path-actions"', text)
                self.assertIn('version = "0.4.0"', text)
                self.assertRegex(text, r'min_herdr_version = "\d+\.\d+\.\d+"')
                self.assertIn(f'platforms = ["{platform}"]', text)

    def test_action_ids_are_unique_and_referenced_scripts_exist(self):
        action_sets = []
        for platform, manifest in MANIFESTS.items():
            with self.subTest(platform=platform):
                text = manifest.read_text(encoding="utf-8")
                action_ids = re.findall(r'(?m)^id = "([^"]+)"$', text)
                self.assertEqual(len(action_ids), len(set(action_ids)))
                action_sets.append(action_ids)

                interpreter = "python" if platform == "windows" else "python3"
                scripts = re.findall(
                    rf'command = \["{interpreter}", "([^"]+)"(?:, [^]]+)?\]',
                    text,
                )
                self.assertTrue(scripts)
                for script in scripts:
                    self.assertTrue((manifest.parent / script).resolve().is_file(), script)

        self.assertEqual(action_sets[0], action_sets[1])


if __name__ == "__main__":
    unittest.main()

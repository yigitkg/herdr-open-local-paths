import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "herdr-plugin.toml"


class ManifestTests(unittest.TestCase):
    def test_release_identity_and_runtime_contract(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn('id = "yigitkg.local-path-actions"', text)
        self.assertIn('version = "0.3.0"', text)
        self.assertRegex(text, r'min_herdr_version = "\d+\.\d+\.\d+"')
        self.assertIn('platforms = ["linux"]', text)

    def test_action_ids_are_unique_and_referenced_scripts_exist(self):
        text = MANIFEST.read_text(encoding="utf-8")
        action_ids = re.findall(r'(?m)^id = "([^"]+)"$', text)
        self.assertEqual(len(action_ids), len(set(action_ids)))
        scripts = re.findall(r'command = \["python3", "([^"]+)"(?:, [^]]+)?\]', text)
        self.assertTrue(scripts)
        for script in scripts:
            self.assertTrue((ROOT / script).is_file(), script)


if __name__ == "__main__":
    unittest.main()

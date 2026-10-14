"""Offline patch compatibility checks; no browser or credentials required."""
import ast
import unittest
import zipfile
from pathlib import Path
import fix_edit_image_echo as patch

ROOT = Path(__file__).resolve().parents[1]


class PatchCompatibilityTests(unittest.TestCase):
    def source(self):
        with zipfile.ZipFile(ROOT / 'credentials.zip') as archive:
            return archive.read('arena_generator.py').decode('utf-8')

    def verify(self, source):
        result, applied, _ = patch.apply_patch(source)
        ast.parse(result)
        self.assertTrue(applied)
        again, applied, _ = patch.apply_patch(result)
        self.assertEqual(again, result)
        self.assertEqual(applied, [])
        self.assertIn('self._last_upload_path = image_path', result)
        return result

    def test_current_generator(self):
        self.verify(self.source())

    def test_legacy_generator_without_task_hook(self):
        source = self.source().replace(
            '    def _begin_generation(self):\n        self._last_upload_path = None\n\n', '')
        source = source.replace('        self._last_upload_path = image_path\n', '')
        result = self.verify(source)
        tree = ast.parse(result)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ArenaGenerator')
        capture = next(node for node in cls.body if node.name == 'start_capture')
        code = ast.get_source_segment(result, capture)
        for field in ('_last_upload_path', '_upload_digests', '_reference_media'):
            self.assertIn('self.' + field + ' =', code)


if __name__ == '__main__':
    unittest.main()

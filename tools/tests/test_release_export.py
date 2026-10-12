"""A source release includes public inputs and refuses ambiguous or private content."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from check_release import MANIFEST, MODULES, ROOT_FILES, SKILLS, check_release
from prepare_release import prepare_release


class ReleaseExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for relative in ROOT_FILES:
            self.write(relative, 'public\n')
        for skill in SKILLS:
            self.write('.agents/skills/' + skill + '/SKILL.md', 'public skill\n')
        for relative in ('tools/check_release.py', 'tools/prepare_release.py', 'tools/tests/test_release_export.py'):
            self.write(relative, 'public tool\n')
        for module in MODULES:
            self.write(module + '/pyproject.toml', '[project]\nversion="0.1.0"\nlicense="Apache-2.0"\n')
            self.write(module + '/src/__init__.py', '')
            self.write(module + '/.env.example', 'TOKEN=\n')
        for family in ('run', 'report', 'state', 'usage'):
            self.write('benchmark/src/schemas/benchmark-' + family + '-v1.schema.json', '{}\n')
            self.write('viewer/src/schemas/' + family + '-v1.json', '{}\n')
        self.write('benchmark/examples/skills/test-driven-development/UPSTREAM.json', '{"files":[]}\n')
        self.write('basket_collector/prompts/system.jinja2', 'public prompt\n')
        self.write('viewer/ops/trash-lifecycle.example.json', '{}\n')

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_deterministic_inventory_excludes_operator_state(self):
        self.write('.env', 'private')
        self.write('benchmark/output/private.json', 'private')
        self.write('.agents/private-session.json', 'private')
        self.write('benchmark/src/__pycache__/cache.pyc', 'transient')
        first, second = self.root / 'first', self.root / 'second'
        self.assertEqual(prepare_release(self.source, first), prepare_release(self.source, second))
        self.assertEqual((first / MANIFEST).read_bytes(), (second / MANIFEST).read_bytes())
        paths = [r['path'] for r in json.loads((first / MANIFEST).read_bytes())['files']]
        self.assertIn('LICENSE', paths)
        self.assertIn('basket_collector/.env.example', paths)
        self.assertIn('basket_collector/prompts/system.jinja2', paths)
        self.assertIn('viewer/ops/trash-lifecycle.example.json', paths)
        self.assertEqual(sum(p.startswith('.agents/') for p in paths), 4)
        self.assertNotIn('benchmark/LICENSE', paths)
        self.assertFalse((first / '.env').exists())

    def test_existing_destination_and_in_source_destination_are_untouched(self):
        destination = self.root / 'existing'
        destination.mkdir()
        sentinel = destination / 'keep'
        sentinel.write_text('keep')
        for target in (destination, self.source / 'export'):
            with self.assertRaises(ValueError):
                prepare_release(self.source, target)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_selected_private_file_and_symlink_are_rejected(self):
        secret = self.write('benchmark/examples/auth.json', 'private')
        with self.assertRaises(ValueError):
            prepare_release(self.source, self.root / 'private')
        secret.unlink()
        (self.source / 'benchmark/examples/linked').symlink_to(self.root)
        with self.assertRaises(ValueError):
            prepare_release(self.source, self.root / 'linked')
        self.assertFalse((self.root / 'linked').exists())

    def test_verifier_rejects_changed_or_unregistered_files(self):
        destination = self.root / 'export'
        prepare_release(self.source, destination)
        extra = destination / 'unexpected'
        extra.write_text('extra')
        with self.assertRaises(ValueError):
            check_release(destination)
        extra.unlink()
        (destination / 'LICENSE').write_text('modified')
        with self.assertRaises(ValueError):
            check_release(destination)

    def test_failed_promotion_removes_only_our_destination(self):
        destination = self.root / 'export'
        import os
        original = os.rename
        calls = []

        def fail_after_first(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError('promotion failed')
            original(source, target)

        with patch('prepare_release.os.rename', side_effect=fail_after_first):
            with self.assertRaises(OSError):
                prepare_release(self.source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(calls), 2)
        self.assertFalse(list(self.root.glob('.export-export-*')))


if __name__ == '__main__':
    unittest.main()

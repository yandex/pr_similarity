"""A source release includes public inputs and refuses ambiguous or private content."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.check_release import MANIFEST, MODULES, ROOT_FILES, SKILLS, check_release
from tools.prepare_release import prepare_release


class ReleaseExportTest(unittest.TestCase):
    """Check export inventory, immutable destination handling and rollback."""

    def setUp(self) -> None:
        """Create a minimal valid source tree in an isolated temporary directory."""
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
        for family, consumer, version in (
            ('run', 'run', 2),
            ('report', 'report', 2),
            ('state', 'state', 2),
            ('manifest', 'archive', 2),
            ('usage', 'usage', 1),
            ('basket', 'basket', 1),
            ('basket', 'basket', 2),
            ('basket', 'basket', 3),
        ):
            self.write(f'benchmark/src/schemas/benchmark-{family}-v{version}.schema.json', '{}\n')
            self.write(f'viewer/src/schemas/{consumer}-v{version}.json', '{}\n')
        self.write('basket_collector/src/schemas/basket-v3.schema.json', '{}\n')
        self.write('benchmark/examples/skills/test-driven-development/UPSTREAM.json', '{"files":[]}\n')
        self.write('basket_collector/prompts/system.jinja2', 'public prompt\n')
        self.write('viewer/ops/trash-lifecycle.example.json', '{}\n')
        self.write('benchmark/docs/configuration.md', 'Public configuration guide.\n')
        self.write('benchmark/examples/full.yaml', 'schema_version: 1\n')

    def write(self, relative: str, content: str) -> Path:
        """Write fixture text at a source-relative path and return the file path."""
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_deterministic_inventory_excludes_operator_state(self) -> None:
        """Export identical manifests while excluding private and transient files."""
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
        self.assertIn('benchmark/docs/configuration.md', paths)
        self.assertIn('benchmark/examples/full.yaml', paths)
        self.assertEqual(sum(p.startswith('.agents/') for p in paths), 4)
        self.assertNotIn('benchmark/LICENSE', paths)
        self.assertFalse((first / '.env').exists())

    def test_runtime_source_package_does_not_admit_operator_runtime(self) -> None:
        """Keep the runtime source package but reject operator runtime directories."""
        self.write('basket_collector/src/runtime/__init__.py', '"""Execution helpers."""\n')
        destination = self.root / 'source-package'
        prepare_release(self.source, destination)
        self.assertTrue((destination / 'basket_collector/src/runtime/__init__.py').is_file())
        self.write('basket_collector/examples/runtime/private.json', '{}\n')
        with self.assertRaises(ValueError):
            prepare_release(self.source, self.root / 'private-runtime')

    def test_existing_destination_and_in_source_destination_are_untouched(self) -> None:
        """Reject unsafe destinations without changing their existing contents."""
        destination = self.root / 'existing'
        destination.mkdir()
        sentinel = destination / 'keep'
        sentinel.write_text('keep')
        for target in (destination, self.source / 'export'):
            with self.assertRaises(ValueError):
                prepare_release(self.source, target)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_source_release_does_not_require_an_actions_workflow(self) -> None:
        """Accept a standalone source release without a CI workflow."""
        (self.source / '.github/workflows/ci.yml').unlink(missing_ok=True)
        destination = self.root / 'without-ci'
        prepare_release(self.source, destination)
        self.assertEqual(check_release(destination)['version'], '0.1.0')
        self.assertFalse((destination / '.github').exists())

    def test_manifest_binds_the_product_release_version(self) -> None:
        """Reject a manifest version that disagrees with module metadata."""
        destination = self.root / 'versioned'
        prepare_release(self.source, destination)
        path = destination / MANIFEST
        manifest = json.loads(path.read_bytes())
        self.assertEqual(manifest['release_version'], '0.1.0')
        manifest['release_version'] = '0.2.0'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'release version'):
            check_release(destination)

    def test_selected_private_file_and_symlink_are_rejected(self) -> None:
        """Reject private files and links in selected source subtrees."""
        secret = self.write('benchmark/examples/auth.json', 'private')
        with self.assertRaises(ValueError):
            prepare_release(self.source, self.root / 'private')
        secret.unlink()
        (self.source / 'benchmark/examples/linked').symlink_to(self.root)
        with self.assertRaises(ValueError):
            prepare_release(self.source, self.root / 'linked')
        self.assertFalse((self.root / 'linked').exists())

    def test_verifier_rejects_changed_or_unregistered_files(self) -> None:
        """Detect both additional files and modified inventoried content."""
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

    def test_failed_promotion_removes_only_our_destination(self) -> None:
        """Remove reserved output and staging when promotion fails partway."""
        destination = self.root / 'export'
        original = os.rename
        calls = []

        def fail_after_first(source: Path, target: Path) -> None:
            """Simulate promotion failure after moving one staged entry."""
            calls.append(source)
            if len(calls) == 2:
                raise OSError('promotion failed')
            original(source, target)

        with patch('tools.prepare_release.os.rename', side_effect=fail_after_first):
            with self.assertRaises(OSError):
                prepare_release(self.source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(calls), 2)
        self.assertFalse(list(self.root.glob('.export-export-*')))


if __name__ == '__main__':
    unittest.main()

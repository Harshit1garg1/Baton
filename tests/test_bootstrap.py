import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/baton/scripts/bootstrap.py'
spec = importlib.util.spec_from_file_location('bootstrap', SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)

    def test_install_is_idempotent_and_preserves_board_and_ignore(self):
        collab = self.project / '.collab'
        collab.mkdir()
        board = collab / 'state-board.json'
        board.write_bytes(b'{"tasks":[{"taskId":"real-task"}]}')
        session = collab / '.local-session.json'
        session.write_bytes(b'{"projectKey":"fixture-only"}')
        ignore = self.project / '.gitignore'
        ignore.write_bytes(b'node_modules/')
        first = bootstrap.install(self.project)
        bootstrap.install(self.project)
        self.assertEqual(first.read_bytes(), SCRIPT.with_name('collab_engine.py').read_bytes())
        self.assertEqual(board.read_bytes(), b'{"tasks":[{"taskId":"real-task"}]}')
        self.assertEqual(session.read_bytes(), b'{"projectKey":"fixture-only"}')
        self.assertEqual(ignore.read_bytes(), b'node_modules/\n/.collab/\n')
        self.assertFalse((collab / 'config.json').exists())

    def test_different_engine_is_not_replaced(self):
        collab = self.project / '.collab'
        collab.mkdir()
        engine = collab / 'collab_engine.py'
        engine.write_bytes(b'# existing engine with local changes\n')
        with self.assertRaisesRegex(ValueError, 'Existing engine differs'):
            bootstrap.install(self.project)
        self.assertEqual(engine.read_bytes(), b'# existing engine with local changes\n')
        self.assertFalse((self.project / '.gitignore').exists())

    def test_symlinked_runtime_is_refused(self):
        outside = self.project / 'outside'
        outside.mkdir()
        try:
            (self.project / '.collab').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable for this account.')
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            bootstrap.install(self.project)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

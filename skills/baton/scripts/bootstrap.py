#!/usr/bin/env python3
"""Install the bundled engine into one project without replacing existing state."""
import argparse
from pathlib import Path


def install(project):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    collab = project / '.collab'
    target = collab / 'collab_engine.py'
    ignore = project / '.gitignore'
    if collab.is_symlink() or target.is_symlink() or ignore.is_symlink():
        raise ValueError('Refusing to install through a symlinked .collab, engine, or .gitignore.')
    source = Path(__file__).with_name('collab_engine.py').read_bytes()
    if target.exists() and target.read_bytes() != source:
        raise ValueError(f'Existing engine differs: {target}. Back it up and review the upgrade before replacing it.')
    previous = ignore.read_bytes() if ignore.exists() else b''
    collab.mkdir(exist_ok=True)
    if not target.exists():
        with target.open('xb') as stream:
            stream.write(source)
    if not any(line.strip() in (b'.collab/', b'/.collab/') for line in previous.splitlines()):
        with ignore.open('ab') as stream:
            if previous and not previous.endswith(b'\n'):
                stream.write(b'\n')
            stream.write(b'/.collab/\n')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='.', help='Existing project folder (default: current directory)')
    args = parser.parse_args()
    try:
        target = install(args.project)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'[INSTALL_ERROR] {exc}\n')
    print(f'[INSTALLED] {target}')
    print('Existing board, configuration and task files were preserved. Run InitFresh only for a new workspace.')


if __name__ == '__main__':
    main()

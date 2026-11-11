"""Install packaged notebooks without overwriting notebooks or user evidence."""
import argparse
from importlib import resources
from pathlib import Path


def install_notebooks(target_dir=None, *, include_legacy=False):
    """Copy missing notebooks only; retain existing files, edits and subdirectories.

    The phase-1 baseline notebook will be added in step 1.7. Historical notebooks
    are opt-in and are installed under legacy/, never mixed into the baseline.
    """
    source = resources.files('pynq_acoustic_camera.data').joinpath('notebooks')
    if target_dir is None:
        root = Path('/home/xilinx/jupyter_notebooks')
        target = root / 'pynq_acoustic_camera' if root.is_dir() else Path.cwd() / 'pynq_acoustic_camera_notebooks'
    else:
        target = Path(target_dir).expanduser().absolute()
    target.mkdir(parents=True, exist_ok=True)
    report = {'destination': str(target), 'installed': [], 'preserved': [],
              'legacy_included': include_legacy}

    def copy_tree(node, relative=Path()):
        for child in sorted(node.iterdir(), key=lambda x: x.name):
            if child.name == 'legacy' and not include_legacy:
                continue
            path = relative / child.name
            if child.is_dir():
                copy_tree(child, path)
            elif child.name.endswith('.ipynb'):
                dest = target / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                payload = child.read_bytes()
                try:
                    with dest.open('xb') as out:
                        out.write(payload)
                except FileExistsError:
                    report['preserved'].append(path.as_posix())
                else:
                    report['installed'].append(path.as_posix())
    copy_tree(source)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target_dir', nargs='?')
    parser.add_argument('--include-legacy', action='store_true',
                        help='Also copy historical XADC notebooks, which are not validated for this release')
    args = parser.parse_args()
    report = install_notebooks(args.target_dir, include_legacy=args.include_legacy)
    print('Notebook directory: ' + report['destination'])
    print('Installed: {}; existing files preserved: {}'.format(
        len(report['installed']), len(report['preserved'])))
    if not report['installed']:
        print('No new notebooks copied. The baseline notebook is prepared in step 1.7.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

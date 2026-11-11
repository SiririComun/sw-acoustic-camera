from pathlib import Path
from setuptools import setup, find_packages

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / 'pynq_acoustic_camera'
# Every runtime resource is inside the import package; no ../ package-data paths.
resource_files = [p.relative_to(PACKAGE).as_posix()
                  for p in sorted((PACKAGE / 'data').rglob('*'))
                  if p.is_file() and p.suffix in {'.json', '.ipynb', '.md'}]

setup(
    name='pynq-acoustic-camera',
    version='0.1.0rc1',
    author='Juan Pablo Sánchez (SiririComun)',
    description='Pinned overlay distribution for the PYNQ-Z2 acoustic camera',
    long_description=(ROOT / 'README.md').read_text(encoding='utf-8'),
    long_description_content_type='text/markdown',
    url='https://github.com/SiririComun/sw-acoustic-camera',
    packages=find_packages(include=['pynq_acoustic_camera', 'pynq_acoustic_camera.*']),
    python_requires='>=3.9',
    install_requires=[
        'numpy>=1.19.0', 'scipy>=1.7.0', 'plotly>=5.10.0,<6.0.0',
        'ipywidgets>=8.0.0', 'pandas>=1.3.0', 'openpyxl>=3.0.0',
    ],
    package_data={'pynq_acoustic_camera': resource_files},
    include_package_data=False,
    entry_points={'console_scripts': [
        'pynq-acoustic-camera-notebooks=pynq_acoustic_camera.notebooks:main',
    ]},
    classifiers=['Programming Language :: Python :: 3',
                 'License :: OSI Approved :: MIT License',
                 'Operating System :: POSIX :: Linux'],
)

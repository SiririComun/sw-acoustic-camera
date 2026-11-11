#!/usr/bin/env python3
"""Verify the installed distribution outside its checkout; no real FPGA access."""
import argparse
import copy
import hashlib
from importlib import metadata, resources
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import pynq_acoustic_camera as package
import pynq_acoustic_camera.loader as loader
from pynq_acoustic_camera import HardwareLoader as Loader
from pynq_acoustic_camera.notebooks import install_notebooks


class DistributionChecks(unittest.TestCase):
    assets = None
    allow_network = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Loader.get_hardware_config()
        self.base = self.root / 'cache'
        self.entry = Loader._cache_path(self.config, self.base)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        self.entry.mkdir(parents=True)
        for name in ['pynq_z2.bit', 'pynq_z2.hwh', 'build-manifest.json']:
            shutil.copyfile(self.assets / name, self.entry / name)

    def transport(self, url, destination, maximum):
        source = self.assets / url.rsplit('/', 1)[1]
        data = source.read_bytes()
        self.assertLessEqual(len(data), maximum)
        destination.write_bytes(data)

    def test_installed_identity_and_lazy_import(self):
        dist = metadata.distribution('pynq-acoustic-camera')
        self.assertEqual(package.__version__, dist.version)
        self.assertEqual(Path(package.__file__).resolve(),
                         Path(dist.locate_file('pynq_acoustic_camera/__init__.py')).resolve())
        for name in ['pynq', 'numpy', 'scipy', 'plotly', 'ipywidgets', 'pandas']:
            self.assertNotIn(name, sys.modules)

    def test_packaged_resources(self):
        data = resources.files('pynq_acoustic_camera.data')
        self.assertEqual(json.loads(data.joinpath('hardware.json').read_text()), self.config)
        self.assertIn('name', json.loads(data.joinpath('profiles').joinpath('active_buzzer_profile.json').read_text()))
        notebook = json.loads(data.joinpath('notebooks').joinpath('01_baseline.ipynb').read_text())
        self.assertEqual(notebook['nbformat'], 4)
        self.assertTrue(all(c.get('execution_count') is None and not c.get('outputs')
                            for c in notebook['cells'] if c['cell_type'] == 'code'))

    def test_config_missing_never_uses_cwd(self):
        (self.root / 'hardware.json').write_text(json.dumps(self.config))
        before = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(loader.resources, 'files', return_value=self.root / 'missing'):
                with self.assertRaises(loader.HardwareConfigError): Loader.get_hardware_config()
        finally:
            os.chdir(before)

    def test_config_invalid_and_incompatible(self):
        folder = self.root / 'config';folder.mkdir()
        cases = ['{', '{}', '{"schema_version":1,"schema_version":1}']
        for field, value in [('repository','../old'),('release_tag','../../other'),
                             ('board','zedboard'),('manifest_sha256','bad'),
                             ('loadable',False),('expected_interface_id','acoustic-camera-capture/1.0')]:
            cfg=copy.deepcopy(self.config);cfg[field]=value;cases.append(json.dumps(cfg))
        for value in cases:
            with self.subTest(value=value[:80]):
                (folder/'hardware.json').write_text(value)
                with patch.object(loader.resources,'files',return_value=folder):
                    with self.assertRaises(loader.HardwareConfigError):Loader.get_hardware_config()

    def test_reject_version_override_before_download(self):
        with patch.object(loader,'_download') as download:
            with self.assertRaises(loader.HardwareConfigError):Loader.get_overlay_path('v9.9.9',self.base)
            download.assert_not_called()

    def test_cache_identity_and_reuse_without_network(self):
        self.fixture()
        with patch.object(loader,'_download',side_effect=AssertionError('Unexpected network')):
            bit=Loader.get_overlay_path(download_dir=self.base)
            self.assertEqual(bit.parent,self.entry)
            self.assertEqual(Loader.get_manifest(download_dir=self.base)['source_commit'],self.config['expected_source_commit'])
        self.assertIn('pynq_acoustic_camera',bit.parts)
        self.assertIn(self.config['manifest_sha256'],bit.parts)

    def test_corruption_rejected_for_each_artifact(self):
        self.fixture()
        for name in ['build-manifest.json','pynq_z2.bit','pynq_z2.hwh']:
            p=self.entry/name;original=p.read_bytes();p.write_bytes(b'broken')
            with self.subTest(name=name), patch.object(loader,'_download') as download:
                with self.assertRaises(loader.HardwareIntegrityError):Loader.get_overlay_path(download_dir=self.base)
                download.assert_not_called()
            p.write_bytes(original)

    def test_missing_handoff_and_linked_bit_rejected(self):
        self.fixture();hwh=self.entry/'pynq_z2.hwh';hwh.unlink()
        with self.assertRaises(loader.HardwareIntegrityError):Loader.get_overlay_path(download_dir=self.base)
        shutil.copyfile(self.assets/hwh.name,hwh)
        bit=self.entry/'pynq_z2.bit';bit.unlink();bit.symlink_to(self.assets/bit.name)
        with self.assertRaises(loader.HardwareIntegrityError):Loader.get_overlay_path(download_dir=self.base)

    def test_new_download_promotes_complete_pair(self):
        with patch.object(loader,'_download',side_effect=self.transport) as download:
            bit=Loader.get_overlay_path(download_dir=self.base)
        self.assertEqual(download.call_count,3)
        self.assertEqual(bit.parent,self.entry)
        self.assertFalse(any(p.name.startswith('.') for p in self.entry.parent.iterdir()))

    def test_forced_download_repairs_only_this_entry(self):
        self.fixture();(self.entry/'pynq_z2.bit').write_bytes(b'broken')
        other=self.base/'old-project-evidence';other.mkdir(parents=True);(other/'keep.txt').write_text('keep')
        with patch.object(loader,'_download',side_effect=self.transport):
            bit=Loader.get_overlay_path(download_dir=self.base,force_download=True)
        self.assertEqual(hashlib.sha256(bit.read_bytes()).hexdigest(),
                         hashlib.sha256((self.assets/bit.name).read_bytes()).hexdigest())
        self.assertEqual((other/'keep.txt').read_text(),'keep')

    def test_failed_force_preserves_previous_valid_pair(self):
        self.fixture();before={p.name:p.read_bytes() for p in self.entry.iterdir()}
        with patch.object(loader,'_download',side_effect=loader.HardwareDownloadError('simulated outage')):
            with self.assertRaises(loader.HardwareDownloadError):Loader.get_overlay_path(download_dir=self.base,force_download=True)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.entry.iterdir()})

    def test_partial_or_wrong_download_never_becomes_cache(self):
        def wrong(url,destination,maximum):destination.write_bytes(b'invalid')
        with patch.object(loader,'_download',side_effect=wrong):
            with self.assertRaises(loader.HardwareIntegrityError):Loader.get_overlay_path(download_dir=self.base)
        self.assertFalse(self.entry.exists())
        calls=[]
        def partial(url,destination,maximum):
            calls.append(url)
            if len(calls)>1:raise loader.HardwareDownloadError('simulated partial transfer')
            self.transport(url,destination,maximum)
        with patch.object(loader,'_download',side_effect=partial):
            with self.assertRaises(loader.HardwareDownloadError):Loader.get_overlay_path(download_dir=self.base)
        self.assertFalse(self.entry.exists())

    def test_generic_overlay_simulation_and_corrupt_rejection(self):
        self.fixture();calls=[]
        def fake_overlay(path,**kwargs):calls.append((path,kwargs));return object()
        with patch.dict(sys.modules,{'pynq':types.SimpleNamespace(Overlay=fake_overlay)}),\
             patch.object(Loader,'get_board_name',return_value='pynq_z2'):
            Loader.load_overlay(download_dir=self.base)
            self.assertEqual(len(calls),1)
            (self.entry/'pynq_z2.bit').write_bytes(b'broken')
            with self.assertRaises(loader.HardwareIntegrityError):Loader.load_overlay(download_dir=self.base)
            self.assertEqual(len(calls),1)

    def test_unknown_or_conflicting_board_prevents_programming(self):
        with patch.object(Loader,'get_board_name',return_value=None):
            with self.assertRaises(loader.HardwareConfigError):Loader.load_overlay(download_dir=self.base)
        with patch.object(Loader,'get_board_name',return_value='zedboard'):
            with self.assertRaises(loader.HardwareConfigError):Loader.load_overlay(board='pynq_z2',download_dir=self.base)

    def test_notebook_installation_preserves_edits_and_evidence(self):
        target=self.root/'notebooks'
        first=install_notebooks(target)
        self.assertEqual(first['installed'],['01_baseline.ipynb'])
        (target/'01_baseline.ipynb').write_text('user edit')
        (target/'my_notebook.ipynb').write_text('user notebook')
        (target/'evidence').mkdir();(target/'evidence/results.json').write_text('user evidence')
        again=install_notebooks(target)
        self.assertEqual(again['preserved'],['01_baseline.ipynb'])
        self.assertEqual((target/'01_baseline.ipynb').read_text(),'user edit')
        self.assertEqual((target/'my_notebook.ipynb').read_text(),'user notebook')
        self.assertEqual((target/'evidence/results.json').read_text(),'user evidence')
        legacy=install_notebooks(target,include_legacy=True)
        self.assertEqual(len(legacy['installed']),10)
        self.assertTrue(all(x.startswith('legacy/') for x in legacy['installed']))

    def test_live_pinned_release_download(self):
        if not self.allow_network:self.skipTest('Network verification not requested')
        bit=Loader.get_overlay_path(download_dir=self.base)
        self.assertEqual(bit.stat().st_size,4045672)
        self.assertEqual(Loader.get_manifest(download_dir=self.base)['interface_id'],'legacy-xadc-reference/1')


class RecordingResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.records=[]
    def startTest(self,test):self.started=time.monotonic();super().startTest(test)
    def record(self,test,status,detail=None):
        self.records.append({'name':test.id(),'status':status,'seconds':round(time.monotonic()-self.started,4),'detail':detail})
    def addSuccess(self,test):super().addSuccess(test);self.record(test,'pass')
    def addFailure(self,test,err):super().addFailure(test,err);self.record(test,'fail',self._exc_info_to_string(err,test))
    def addError(self,test,err):super().addError(test,err);self.record(test,'error',self._exc_info_to_string(err,test))
    def addSkip(self,test,reason):super().addSkip(test,reason);self.record(test,'skip',reason)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets-dir',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--network',action='store_true')
    args=parser.parse_args();DistributionChecks.assets=args.assets_dir.resolve();DistributionChecks.allow_network=args.network
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordingResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(DistributionChecks))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    report={'python':sys.version,'executable':sys.executable,'module_path':package.__file__,'distribution_version':metadata.version('pynq-acoustic-camera'),'results':result.records,'successful':result.wasSuccessful(),'real_pynq_imported':False,'real_fpga_programmed':False}
    with args.report.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())

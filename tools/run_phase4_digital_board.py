#!/usr/bin/env python3
"""Phase4.7: explicit programming and digital checks on PYNQ-Z2, no rewiring.

Use the root PYNQ interpreter. Bundle/runner paths are explicit; every attempt
uses a new evidence folder. The accepted RAW candidate is never overwritten.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, os, subprocess, sys, traceback, uuid, zipfile

DELIVERY_SHA256 = 'b4618c79ea85a9d0d514810ddf15081c0e7110f4d51e168e08043ebffb37596a'

DIAGNOSTICS = r'''
from pynq import allocate
from pynq_acoustic_camera import CaptureError, PdmIdentityError
from pynq_acoustic_camera.raw_capture import _UNSAFE_BUFFERS
checks = {}
c, d = array._control, array._dma
array.enable_clock()
array.wait_filter_ready()

def exact(n=17, fmt='pcm_s24_in_s32'):
    result = array.capture(n, format=fmt, source='pattern')
    expected = (16*np.arange(n,dtype=np.int32)[:,None] + np.arange(4,dtype=np.int32)) if fmt == 'pcm_s24_in_s32' else np.arange(n,dtype=np.uint32)
    np.testing.assert_array_equal(result.samples, expected)
    return result.metadata

class Probe:
    def __init__(self, mmio): self.mmio, self.writes = mmio, 0
    def read(self, offset): return self.mmio.read(offset)
    def write(self, offset, value): self.writes += 1; raise AssertionError('Unexpected write')
p,q = Probe(c),Probe(d)
probe = AcousticArray(p,q,EXPECTED_BUILD_ID,allocator=allocate)
for kwargs in ({'n_frames':0}, {'n_frames':65537}, {'format':'pcm'}):
    try: probe.capture(**kwargs)
    except ValueError: pass
    else: raise AssertionError('Invalid arguments accepted')
wrong = ('0' if EXPECTED_BUILD_ID[0] != '0' else '1') + EXPECTED_BUILD_ID[1:]
try: AcousticArray(p,q,wrong,allocator=allocate)
except PdmIdentityError: pass
else: raise AssertionError('Wrong identity accepted')
assert p.writes == q.writes == 0
checks['arguments_identity'] = {'result':'pass','writes':0}

def arm():
    buffer = allocate(shape=(65536,4),dtype=np.int32)
    try:
        assert buffer.physical_address % 4 == 0 and buffer.physical_address + buffer.nbytes <= 2**32
        buffer.flush(); array._clear()
        c.write(0x30,1); c.write(0x34,1); c.write(0x38,65536)
        assert c.read(0x2C)==0
        d.write(0x30,1)
        array._wait(lambda: not d.read(0x34)&1,1,'Diagnostic DMA start failed')
        d.write(0x48,int(buffer.physical_address)); d.write(0x4C,0)
        d.write(0x58,buffer.nbytes); c.write(0x24,1)
        assert c.read(0x28)&2,'Controlled injection requires BUSY'
        return buffer
    except BaseException:
        try: array.recover()
        except BaseException: _UNSAFE_BUFFERS.append(buffer)
        else: buffer.freebuffer()
        raise

try:
    for mode in ('busy_write','abort'):
        ready_before=array.filter_ready
        buffer=arm(); recovered=False
        try:
            if mode=='busy_write':
                c.write(0x34,0)
                assert c.read(0x2C)&1 and c.read(0x34)==1
            else:
                c.write(0x24,2)
                assert c.read(0x2C)&4
            before=array._diagnostics()
            array.recover(); recovered=True
            assert c.read(0x20)==1 and c.read(0x30)==1 and c.read(0x34)==1 and c.read(0x38)==65536
            assert ready_before and array.filter_ready,'Local recovery must preserve settled filter'
            checks[mode]={'result':'pass','injected_status':before,
                          'filter_ready_preserved':True,'next_capture':exact()}
        finally:
            if recovered: buffer.freebuffer()
            else: _UNSAFE_BUFFERS.append(buffer)
    try: array.capture(65536,format='pcm_s24_in_s32',source='pattern',timeout=0.000001)
    except CaptureError as error:
        assert 'timed out' in error.diagnostics.get('cause','')
        assert error.diagnostics['recovery']=='ok'
        checks['timeout']={'result':'pass','diagnostics':error.diagnostics,'next_capture':exact()}
    else: raise AssertionError('Controlled timeout did not fail')
    checks['format_switch']={'result':'pass','raw':exact(fmt='raw_pdm'),'pcm':exact()}
    array.disable_clock()
    # Explicit second load of the exact verified pair checks reset and repeatability.
    for path,digest in ((BIT_FILE,EXPECTED_BIT_SHA256),(BIT_FILE.with_suffix('.hwh'),EXPECTED_HWH_SHA256)):
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    overlay.download()
    array=AcousticArray.from_overlay(overlay,EXPECTED_BUILD_ID,provenance=PROVENANCE)
    array._wait(lambda: not c.read(0x28)&2,1,'Reset recovery busy')
    assert not array.clock.clock_enabled and not array.clock.clock_ready and not array.filter_ready
    assert all(array._control.read(x)==0 for x in (0x2C,0x4C,0x50,0x54,0x58))
    array.enable_clock(); array.wait_filter_ready()
    checks['reprogram_reset']={'result':'pass','next_capture':exact()}
finally:
    try:
        if array._control.read(0x28)&2: array.recover()
        array.disable_clock()
        checks['clock_final']={'result':'pass' if not array.clock.clock_enabled else 'fail','enabled':array.clock.clock_enabled}
    except BaseException as cleanup:
        checks['clock_final']={'result':'fail','error':str(cleanup)}
    (EVIDENCE_ROOT/'diagnostics-report.json').write_text(json.dumps(checks,indent=2)+'\n')
assert len(checks)==7 and all(v['result']=='pass' for v in checks.values()),checks
print('PCM_DIGITAL_DIAGNOSTICS_PASS',flush=True)
'''

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path);parser.add_argument('evidence',type=Path)
    parser.add_argument('--run',action='store_true');args=parser.parse_args()
    bundle=args.bundle.resolve()
    assert sha(bundle/'delivery-manifest.json')==DELIVERY_SHA256, 'Delivery manifest differs from reviewed local build'
    delivery=json.loads((bundle/'delivery-manifest.json').read_text())
    for item in delivery['hardware_artifacts']+[delivery['wheel']]:
        p=bundle/item['path']
        assert p.parent==bundle and p.stat().st_size==item['size_bytes'] and sha(p)==item['sha256']
    manifest=bundle/'candidate-manifest.json';assert sha(manifest)==delivery['candidate_manifest_sha256']
    candidate=json.loads(manifest.read_text());assert candidate['build_id']==delivery['build_id']=='b691cc9c9f6249ad938d2063e4767c54'
    if not args.run: print('PREFLIGHT_PASS; no hardware access');return 0
    assert os.geteuid()==0 and Path(sys.prefix).resolve()==Path('/usr/local/share/pynq-venv')
    assert os.environ.get('XILINX_XRT')=='/usr'
    board=Path('/proc/device-tree/chosen/pynq_board').read_bytes().rstrip(b'\0').decode();assert board.lower().replace('-','_')=='pynq_z2'
    attempt=args.evidence.resolve()/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]);attempt.mkdir(parents=True)
    status={'phase':4,'step':'4.7','status':'running','started_utc':datetime.now(timezone.utc).isoformat(),'board':board,'python':sys.executable,'uid':os.geteuid(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'delivery':delivery,'runner_sha256':sha(Path(__file__)),'microphones_connected':True,'wiring':'User-restored Mic0/B2 Mic1/B3, Mic2/B4 Mic3/B7; acoustic identity pending','external_waveform':'not_measured'}
    def save(): (attempt/'status.json').write_text(json.dumps(status,indent=2)+'\n')
    save();print('EVIDENCE_DIRECTORY='+str(attempt),flush=True)
    notebook=None
    try:
        from importlib import metadata,resources
        deps=('pynq','numpy','nbclient','nbformat');before={n:metadata.version(n) for n in deps}
        with (attempt/'pip-install.log').open('w') as log:
            subprocess.run([sys.executable,'-m','pip','install','--force-reinstall','--no-cache-dir','--no-deps',str(bundle/delivery['wheel']['path'])],check=True,stdout=log,stderr=subprocess.STDOUT,timeout=180)
        status['versions']={n:metadata.version(n) for n in deps};assert status['versions']==before
        import pynq_acoustic_camera
        installed=Path(pynq_acoustic_camera.__file__).parent;assert '/site-packages/' in str(installed)
        with zipfile.ZipFile(bundle/delivery['wheel']['path']) as z:
            names=[n for n in z.namelist() if n.startswith('pynq_acoustic_camera/')]
            for name in names: assert z.read(name)==(installed.parent/name).read_bytes(),name
        profile=json.loads((installed/'data/pcm_candidate.json').read_text());assert profile['manifest_sha256']==delivery['candidate_manifest_sha256'] and profile['artifacts']==candidate['artifacts']
        status['installed_package']=str(installed);status['installed_package_files_verified']=len(names);save()
        import nbformat
        from nbclient import NotebookClient
        source=resources.files('pynq_acoustic_camera.data').joinpath('notebooks/04_pcm_capture.ipynb').read_text()
        notebook=nbformat.reads(source,as_version=4)
        for cell in notebook.cells:
            if cell.cell_type=='code':
                cell.source=cell.source.replace('RUN_HARDWARE = False','RUN_HARDWARE = True').replace('RUN_RAW_CHECK = False','RUN_RAW_CHECK = True').replace('RUN_PATTERN = False','RUN_PATTERN = True')
                cell.source=cell.source.replace("Path('/home/xilinx/acoustic_camera_phase4_candidate/pdm_pcm_capture.bit')",'Path('+repr(str(bundle/'pdm_pcm_capture.bit'))+')')
                cell.source=cell.source.replace("Path('/home/xilinx/acoustic_camera_phase4_evidence')",'Path('+repr(str(attempt))+')')
        program='''import os,sys\nassert os.geteuid()==0 and sys.prefix=="/usr/local/share/pynq-venv"\nassert os.environ.get("XILINX_XRT")=="/usr"\nfrom pynq import Overlay\nfor path,digest in ((BIT_FILE,EXPECTED_BIT_SHA256),(BIT_FILE.with_suffix('.hwh'),EXPECTED_HWH_SHA256)):\n    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest\nprogrammed=Overlay(str(BIT_FILE),download=False)\nprogrammed.download()\nprint('VERIFIED_PCM_PAIR_PROGRAMMED',flush=True)\n'''
        notebook.cells.insert(2,nbformat.v4.new_code_cell(program))
        provenance={'hw_commit':delivery['hardware_source_commit'],'sw_commit':delivery['software_source_commit'],'candidate_manifest_sha256':delivery['candidate_manifest_sha256'],'wheel_sha256':delivery['wheel']['sha256'],'bit_sha256':profile['artifacts'][0]['sha256'],'hwh_sha256':profile['artifacts'][1]['sha256']}
        notebook.cells.insert(4,nbformat.v4.new_code_cell('from dataclasses import asdict\nPROVENANCE='+repr(provenance)+'\narray._provenance.update(PROVENANCE)\n(EVIDENCE_ROOT/"identity.json").write_text(json.dumps(asdict(array.clock.identity),indent=2)+"\\n")\nassert not array.clock.clock_enabled\n'))
        notebook.cells.append(nbformat.v4.new_code_cell(DIAGNOSTICS))
        nbformat.write(notebook,attempt/'04_pcm_digital.input.ipynb');print('NOTEBOOK_START',flush=True)
        NotebookClient(notebook,kernel_name='python3',timeout=120,resources={'metadata':{'path':str(attempt)}}).execute()
        raw=list(attempt.glob('raw-wiring-check-*/session-report.json'));patterns=list(attempt.glob('pcm-pattern-*/session-report.json'));assert len(raw)==len(patterns)==1
        rr=json.loads(raw[0].read_text());pr=json.loads(patterns[0].read_text());diag=json.loads((attempt/'diagnostics-report.json').read_text())
        assert rr['status']==pr['status']=='pass' and len(pr['cases'])==50 and all(c['status']=='pass' for c in pr['cases'])
        assert len(diag)==7 and all(v['result']=='pass' for v in diag.values())
        status.update(status='pass',raw_frames=4096,pattern_blocks=50,pattern_frames=696520,pattern_samples=2786080,diagnostics=list(diag),clock_final='disabled')
    except BaseException as error:
        status.update(status='fail',error=str(error),traceback=traceback.format_exc());print('ERROR='+str(error),flush=True)
    finally:
        if notebook is not None:
            import nbformat
            nbformat.write(notebook,attempt/'04_pcm_digital.executed.ipynb')
        status['finished_utc']=datetime.now(timezone.utc).isoformat();save()
        files=[{'path':str(p.relative_to(attempt)),'size_bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(attempt.rglob('*')) if p.is_file()]
        (attempt/'artifacts.json').write_text(json.dumps(files,indent=2)+'\n')
    print('RESULT='+status['status'],flush=True);return 0 if status['status']=='pass' else 1

if __name__=='__main__': raise SystemExit(main())

#!/usr/bin/env python3
"""Run one explicit phase-4 physical PCM session with the pinned candidate.

Use PYNQ root interpreter through the existing privileged kernel. Creates a
new evidence directory, executes an installed notebook copy, and checks that
capture reports and clock shutdown completed. No cable changes.
"""
from pathlib import Path
from datetime import datetime, timezone
from importlib import metadata, resources
import argparse, hashlib, json, os, sys, traceback, uuid, zipfile

BUNDLE=Path('/home/xilinx/acoustic_camera_phase4_7/candidate')
BASE=Path('/home/xilinx/acoustic_camera_phase4_8/evidence')
EXPECTED_DELIVERY_SHA='b4618c79ea85a9d0d514810ddf15081c0e7110f4d51e168e08043ebffb37596a'

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--kind',required=True,choices=('silence','tone','identity','broadband'))
    a.add_argument('--repetitions',type=int,default=10)
    a.add_argument('--tone-hz',type=int,choices=(1000,2000,3000))
    a.add_argument('--identity-channel',type=int,choices=range(4))
    a.add_argument('--physical-context-json',type=Path)
    args=a.parse_args()
    assert os.geteuid()==0 and Path(sys.prefix).resolve()==Path('/usr/local/share/pynq-venv')
    assert os.environ.get('XILINX_XRT')=='/usr'
    assert 1 <= args.repetitions <= 10
    assert (args.kind=='tone')==(args.tone_hz is not None)
    assert (args.kind=='identity')==(args.identity_channel is not None)
    board=Path('/proc/device-tree/chosen/pynq_board').read_bytes().rstrip(b'\0').decode()
    assert board.lower().replace('-','_')=='pynq_z2'
    context=json.loads(args.physical_context_json.read_text()) if args.physical_context_json else {}
    required=('source','distance_m','orientation','source_duration_seconds','level_setting','position_uncertainty_m','room_notes')
    assert not set(context)-set(required)
    if args.kind!='silence':
        assert all(context.get(k) is not None for k in ('source','distance_m','orientation','source_duration_seconds'))
    assert sha(BUNDLE/'delivery-manifest.json')==EXPECTED_DELIVERY_SHA
    delivery=json.loads((BUNDLE/'delivery-manifest.json').read_text())
    assert delivery['build_id']=='b691cc9c9f6249ad938d2063e4767c54'
    assert sha(BUNDLE/'candidate-manifest.json')==delivery['candidate_manifest_sha256']
    for item in delivery['hardware_artifacts']+[delivery['wheel']]:
        p=BUNDLE/item['path']
        assert p.parent==BUNDLE and p.stat().st_size==item['size_bytes'] and sha(p)==item['sha256']
    import pynq_acoustic_camera
    installed=Path(pynq_acoustic_camera.__file__).parent
    assert '/site-packages/' in str(installed)
    with zipfile.ZipFile(BUNDLE/delivery['wheel']['path']) as z:
        names=[n for n in z.namelist() if n.startswith('pynq_acoustic_camera/')]
        for name in names: assert z.read(name)==(installed.parent/name).read_bytes(),name
    profile=json.loads((installed/'data/pcm_candidate.json').read_text())
    assert profile['manifest_sha256']==delivery['candidate_manifest_sha256'] and profile['artifacts']==delivery['hardware_artifacts']
    attempt=BASE/('run-'+args.kind+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True)
    status={'phase':4,'step':'4.8','status':'running','started_utc':datetime.now(timezone.utc).isoformat(),
            'board':board,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'python':sys.executable,'uid':os.geteuid(),'kind':args.kind,'repetitions':args.repetitions,
            'tone_hz':args.tone_hz,'identity_channel':args.identity_channel,'physical_context':context,
            'delivery':delivery,'runner_sha256':sha(Path(__file__)),'installed_package':str(installed),
            'installed_package_files_verified':len(names),'versions':{n:metadata.version(n) for n in ('pynq','numpy','nbclient','nbformat')},
            'external_waveform':'not_measured'}
    def save(): (attempt/'status.json').write_text(json.dumps(status,indent=2)+'\n')
    save();print('EVIDENCE_DIRECTORY='+str(attempt),flush=True)
    notebook=None
    try:
        import nbformat
        from nbclient import NotebookClient
        source=resources.files('pynq_acoustic_camera.data').joinpath('notebooks/04_pcm_capture.ipynb').read_text()
        notebook=nbformat.reads(source,as_version=4)
        bit=BUNDLE/'pdm_pcm_capture.bit'
        for cell in notebook.cells:
            if cell.cell_type!='code':continue
            cell.source=cell.source.replace('RUN_HARDWARE = False','RUN_HARDWARE = True').replace('RUN_ACOUSTICS = False','RUN_ACOUSTICS = True')
            cell.source=cell.source.replace("Path('/home/xilinx/acoustic_camera_phase4_candidate/pdm_pcm_capture.bit')",'Path('+repr(str(bit))+')')
            cell.source=cell.source.replace("Path('/home/xilinx/acoustic_camera_phase4_evidence')",'Path('+repr(str(attempt))+')')
            cell.source=cell.source.replace("TEST_KIND = 'silence'",'TEST_KIND = '+repr(args.kind))
            cell.source=cell.source.replace('EXPECTED_TONE_HZ = None  # 1000, 2000 o3000 en ejecuciones separadas.','EXPECTED_TONE_HZ = '+repr(args.tone_hz))
            cell.source=cell.source.replace('IDENTITY_CHANNEL = None  # Micrófono al que se aproxima la fuente.','IDENTITY_CHANNEL = '+repr(args.identity_channel))
            cell.source=cell.source.replace('ACOUSTIC_REPETITIONS = 10','ACOUSTIC_REPETITIONS = '+str(args.repetitions))
            if 'PHYSICAL_CONTEXT = {' in cell.source:
                start=cell.source.index('PHYSICAL_CONTEXT = {')
                end=cell.source.index('}\n',start)+2
                cell.source=cell.source[:start]+'PHYSICAL_CONTEXT = '+repr({k:context.get(k) for k in required})+'\n'+cell.source[end:]
            if 'figure.show()' in cell.source:
                cell.source="print('Visualización omitida en ejecución sin pantalla; muestras y análisis guardados')"
        program='''import os,sys\nassert os.geteuid()==0 and sys.prefix=="/usr/local/share/pynq-venv"\nassert os.environ.get("XILINX_XRT")=="/usr"\nfrom pynq import Overlay\nfor path,digest in ((BIT_FILE,EXPECTED_BIT_SHA256),(BIT_FILE.with_suffix('.hwh'),EXPECTED_HWH_SHA256)):\n    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest\nprogrammed=Overlay(str(BIT_FILE),download=False)\nprogrammed.download()\nprint('VERIFIED_PCM_PAIR_PROGRAMMED',flush=True)\n'''
        notebook.cells.insert(2,nbformat.v4.new_code_cell(program))
        notebook.cells.insert(4,nbformat.v4.new_code_cell('from dataclasses import asdict\n(EVIDENCE_ROOT/"identity.json").write_text(json.dumps(asdict(array.clock.identity),indent=2)+"\\n")\nassert not array.clock.clock_enabled\n'))
        nbformat.write(notebook,attempt/'04_pcm_physical.input.ipynb')
        print('NOTEBOOK_START',flush=True)
        NotebookClient(notebook,kernel_name='python3',timeout=180,resources={'metadata':{'path':str(attempt)}}).execute()
        reports=list(attempt.glob('pcm-'+args.kind+'-*/session-report.json'))
        assert len(reports)==1
        report=json.loads(reports[0].read_text())
        assert report['status']=='pass' and len(report['cases'])==args.repetitions
        assert all(c['hardware_clips']==0 for c in report['cases'])
        status.update(status='pass',capture_dir=str(reports[0].parent),blocks=len(report['cases']),
                      frames=48000*len(report['cases']),clock_final='disabled')
    except BaseException as error:
        status.update(status='fail',error=str(error),traceback=traceback.format_exc())
        print('ERROR='+str(error),flush=True)
    finally:
        if notebook is not None:
            import nbformat
            nbformat.write(notebook,attempt/'04_pcm_physical.executed.ipynb')
        status['finished_utc']=datetime.now(timezone.utc).isoformat();save()
        files=[{'path':str(p.relative_to(attempt)),'size_bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(attempt.rglob('*')) if p.is_file()]
        (attempt/'artifacts.json').write_text(json.dumps(files,indent=2)+'\n')
    print('RESULT='+status['status'],flush=True)
    return 0 if status['status']=='pass' else 1

if __name__=='__main__':raise SystemExit(main())

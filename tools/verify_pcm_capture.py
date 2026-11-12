#!/usr/bin/env python3
"""Host-only PCM API, analysis and notebook checks; no board or PYNQ runtime."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_raw_capture import Hardware as RawHardware, BUILD as RAW_BUILD
from pynq_acoustic_camera import AcousticArray, CaptureError, CaptureResult
from pynq_acoustic_camera.pdm_control import PdmIdentityError
from pynq_acoustic_camera.raw_capture import _UNSAFE_BUFFERS
from pynq_acoustic_camera.pcm_analysis import analyze_pcm, estimate_pair_delays, pcm_spectrum

BUILD = 'B691CC9C9F6249AD938D2063E4767C54'


class Hardware(RawHardware):
    def __init__(self, mode=None, reset_fails=False, ready=True):
        super().__init__(mode, reset_fails)
        self.reg.update({8: 0xF, 0x20: 1, 0x28: 0x11 if ready else 1,
                         0x44: 48000, 0x48: 50, 0x58: 0, 0x5C: 48000, 0x60: 6})
        for i in range(4): self.reg[0x10+4*i] = (int(BUILD,16) >> (32*i)) & 0xffffffff

    def write(self, dma, offset, value):
        if not dma and offset == 0x24:
            self.log.append(('control',offset,value))
            if value == 4:
                self.reg.update({0x28: self.reg[0x28] & 0x11, 0x2C: 0, 0x4C: 0, 0x50: 0, 0x54: 0, 0x58: 0})
            elif value == 2:
                self.reg[0x2C] = 4; self.reg[0x28] |= 8
            elif value == 1:
                n = self.reg[0x38]; pcm = self.reg[0x30] == 1; pattern = self.reg[0x34] == 1
                words = (4 if pcm else 1)*n
                assert self.dma[0x58] == words*4 and self.dma[0x30] == 1
                assert self.buffers[-1].flushed
                if pcm and not pattern: assert self.reg[0x28] & 0x10
                if pcm:
                    samples = (16*np.arange(n,dtype=np.int32)[:,None]+np.arange(4,dtype=np.int32)) if pattern else np.tile(np.array([-8388608,-1,0,8388607],dtype=np.int32),(n,1))
                else:
                    samples = np.arange(n,dtype=np.uint32) if pattern else np.full(n,10,dtype=np.uint32)
                self.buffers[-1][:] = samples
                self.reg.update({0x28: 5 | (self.reg[0x28] & 16), 0x4C: n, 0x50: words, 0x58: 2*n if pcm and not pattern else 0})
                if self.mode == 'short': self.dma[0x58] -= 4
                if self.mode == 'words': self.reg[0x50] -= 1
                if self.mode == 'frames': self.reg[0x4C] -= 1
                if self.mode == 'overflow': self.reg.update({0x28: 11, 0x2C: 2, 0x54: 1})
                if self.mode == 'dma': self.dma[0x34] = 0x10
                if self.mode == 'timeout': self.reg[0x28] = 3; self.dma[0x34] = 0
                if self.mode == 'sign': self.buffers[-1][0,0] = 8388608
                if self.mode == 'clips': self.reg[0x58] = 4*n+1
                if self.mode == 'diagnostic_clips': self.reg[0x58] = 1
            return
        super().write(dma,offset,value)
        if not dma and offset == 0x20:
            if value == 0: self.reg[0x28] &= ~0x11
            else: self.reg[0x28] |= 1


class PcmDriverChecks(unittest.TestCase):
    def driver(self,h):
        return AcousticArray(h.control,h.engine,BUILD,allocator=h.allocate,recovery_timeout=0.004,
                             provenance={'test':'host_model'})

    def test_pcm_pattern_five_sizes_exact_shape_copy_and_arming(self):
        h=Hardware(ready=False); a=self.driver(h)
        self.assertEqual(h.log,[])
        for n in (1,2,17,4096,65536):
            result=a.capture(n,format='pcm_s24_in_s32',source='pattern')
            self.assertIsInstance(result,CaptureResult)
            expected=16*np.arange(n,dtype=np.int32)[:,None]+np.arange(4,dtype=np.int32)
            np.testing.assert_array_equal(result.samples,expected)
            self.assertEqual(result.samples.shape,(n,4)); self.assertEqual(result.samples.dtype,np.int32)
            self.assertFalse(np.shares_memory(result.samples,h.buffers[-1]))
            self.assertTrue(h.buffers[-1].freed and h.buffers[-1].flushed and h.buffers[-1].invalidated)
            m=result.metadata
            self.assertEqual(m['bytes'],16*n); self.assertEqual(m['counters']['words'],4*n)
            self.assertEqual(m['timeout_seconds'],max(2,10*n/48000))
            self.assertEqual(m['decimation'],50); self.assertEqual(m['filter_settle_frames'],6)
            self.assertFalse(m['filter_applied']); self.assertFalse(m['filter_ready_at_start'])
            self.assertFalse(m['filter_reset_per_block'])
            self.assertEqual(m['provenance'],{'test':'host_model'})
            json.dumps(m,allow_nan=False)
            start=max(i for i,x in enumerate(h.log) if x==('control',0x24,1))
            arm=max(i for i,x in enumerate(h.log) if x==('dma',0x58,16*n))
            self.assertLess(arm,start)

    def test_pcm_signed_values_clips_no_extra_discard(self):
        h=Hardware();a=self.driver(h)
        result=a.capture(17,format='pcm_s24_in_s32')
        np.testing.assert_array_equal(result.samples,np.tile([-8388608,-1,0,8388607],(17,1)))
        self.assertEqual(result.metadata['counters']['clips'],34)
        self.assertEqual(len(result.samples),17)
        self.assertTrue(result.metadata['filter_applied'])
        self.assertEqual(result.metadata['pcm_normalization_divisor'],8388608)

    def test_raw_on_pcm_hardware_and_accepted_raw_dispatch(self):
        h=Hardware();a=self.driver(h)
        for source in ('pattern','microphones'):
            result=a.capture(17,source=source)
            np.testing.assert_array_equal(result.samples,np.arange(17) if source=='pattern' else np.full(17,10))
            self.assertEqual(result.samples.dtype,np.uint32)
            self.assertEqual(result.metadata['nominal_sample_rate_hz'],2400000)
        old=RawHardware(); legacy=AcousticArray(old.control,old.engine,RAW_BUILD,allocator=old.allocate)
        for n in (1,2,17,4096,65536):
            np.testing.assert_array_equal(legacy.capture(n,source='pattern').samples,np.arange(n))
        writes=len(old.log)
        with self.assertRaises(ValueError): legacy.capture(format='pcm_s24_in_s32')
        self.assertEqual(writes,len(old.log))

    def test_identity_and_constants_rejected_before_writes(self):
        for offset,value in [(0,0),(4,0x20000),(8,3),(12,2),(0x10,0),(0x3C,100),(0x40,2500000),(0x44,44100),(0x48,64),(0x5C,0),(0x60,0)]:
            with self.subTest(offset=offset):
                h=Hardware();h.reg[offset]=value
                with self.assertRaises(PdmIdentityError): self.driver(h)
                self.assertEqual(h.log,[]);self.assertEqual(h.buffers,[])

    def test_invalid_arguments_do_not_write_or_allocate(self):
        h=Hardware();a=self.driver(h)
        for n in (0,65537,True,1.5):
            with self.assertRaises(ValueError): a.capture(n)
        for kwargs in ({'format':'pcm'},{'source':'invalid'},{'timeout':0},{'timeout':True},{'timeout':float('nan')},{'timeout':float('inf')}):
            with self.assertRaises(ValueError): a.capture(**kwargs)
        for t in (0,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError): a.wait_filter_ready(t)
        self.assertEqual(h.log,[]);self.assertEqual(h.buffers,[])

    def test_filter_wait_is_read_only_and_pattern_bypasses_it(self):
        h=Hardware(ready=False);a=self.driver(h)
        with self.assertRaises(CaptureError): a.capture(17,format='pcm_s24_in_s32',timeout=0.003)
        self.assertEqual(h.log,[]);self.assertEqual(h.buffers,[])
        reads=[0]; original=h.control.read
        def delayed(offset):
            if offset==0x28:
                reads[0]+=1
                if reads[0]>=5: h.reg[0x28]|=16
            return original(offset)
        h.control.read=delayed
        self.assertEqual(a.capture(17,format='pcm_s24_in_s32').samples.shape,(17,4))
        self.assertGreaterEqual(reads[0],5)

    def test_preflight_busy_clock_dma_busy_and_stale_errors(self):
        for status,dma in ((0,1),(3,1),(17,0),(17,0x11)):
            h=Hardware();h.reg[0x28]=status;h.dma[0x34]=dma;a=self.driver(h)
            with self.assertRaises(CaptureError): a.capture(17,format='pcm_s24_in_s32')
            self.assertEqual(h.log,[]);self.assertEqual(h.buffers,[])

    def test_incomplete_sign_clips_and_errors_recover_then_capture(self):
        for mode in ('short','words','frames','overflow','dma','timeout','sign','clips','diagnostic_clips'):
            with self.subTest(mode=mode):
                h=Hardware(mode);a=self.driver(h)
                with self.assertRaises(CaptureError) as raised:
                    a.capture(17,format='pcm_s24_in_s32',source='pattern',timeout=0.003)
                self.assertEqual(raised.exception.diagnostics['recovery'],'ok')
                self.assertTrue(h.buffers[-1].freed)
                self.assertEqual(a.capture(2,format='pcm_s24_in_s32',source='pattern').samples.shape,(2,4))

    def test_failed_reset_retains_buffer_and_refuses_reuse(self):
        h=Hardware('timeout',reset_fails=True);a=self.driver(h)
        with self.assertRaises(CaptureError): a.capture(17,format='pcm_s24_in_s32',timeout=0.003)
        self.assertFalse(h.buffers[-1].freed);self.assertIs(_UNSAFE_BUFFERS[-1],h.buffers[-1])
        writes=len(h.log)
        with self.assertRaises(CaptureError): a.capture(1)
        self.assertEqual(writes,len(h.log))

    def test_invalid_dma_address_is_freed_without_hardware_writes(self):
        h=Hardware();allocate=h.allocate
        def bad(shape,dtype):
            buffer=allocate(shape,dtype);buffer.physical_address=2**32-4;return buffer
        a=AcousticArray(h.control,h.engine,BUILD,allocator=bad)
        with self.assertRaises(CaptureError): a.capture(17,format='pcm_s24_in_s32')
        self.assertEqual(h.log,[]);self.assertTrue(h.buffers[-1].freed)

    def test_recovery_preserves_clock_configuration_and_filter(self):
        h=Hardware();a=self.driver(h);h.reg.update({0x20:1,0x30:1,0x34:0,0x38:17,0x28:19})
        a.recover()
        self.assertEqual([h.reg[x] for x in (0x20,0x30,0x34,0x38)],[1,1,0,17])
        self.assertTrue(a.filter_ready)
        a.disable_clock();self.assertFalse(a.filter_ready)

    def test_overlay_hwh_checks_and_success_attach_are_read_only(self):
        h=Hardware()
        class Overlay:
            ip_dict={'acoustic_capture_0':{'phys_addr':0x60000000,'addr_range':65536},
                     'axi_dma_audio':{'phys_addr':0x50000000,'addr_range':65536,'parameters':
                      {'c_include_sg':0,'c_include_mm2s':0,'c_include_s2mm':1,'c_sg_length_width':23,'c_addr_width':32,'c_m_axi_s2mm_data_width':32,'c_s_axis_s2mm_tdata_width':32}},
                     'processing_system7_0':{'parameters':{'C_S_AXI_HP0_DATA_WIDTH':64}}}
            def is_loaded(self): return True
        def mmio(address,length):
            return {0x60000000:h.control,0x50000000:h.engine}[address]
        with patch.dict(sys.modules,{'pynq':types.SimpleNamespace(MMIO=mmio,allocate=h.allocate)}):
            a=AcousticArray.from_overlay(Overlay(),BUILD)
            self.assertTrue(a.filter_ready);self.assertEqual(h.log,[])
        for param,value in [('c_sg_length_width',14),('c_s_axis_s2mm_tdata_width',64)]:
            old=Overlay.ip_dict['axi_dma_audio']['parameters'][param]
            Overlay.ip_dict['axi_dma_audio']['parameters'][param]=value
            with self.assertRaises(PdmIdentityError): AcousticArray.from_overlay(Overlay(),BUILD)
            Overlay.ip_dict['axi_dma_audio']['parameters'][param]=old
        Overlay.ip_dict['processing_system7_0']['parameters']['C_S_AXI_HP0_DATA_WIDTH']=32
        with self.assertRaises(PdmIdentityError): AcousticArray.from_overlay(Overlay(),BUILD)
        self.assertEqual(h.log,[])


class AnalysisChecks(unittest.TestCase):
    def test_tones_dc_and_neighbor_floor_criteria(self):
        fs=48000;t=np.arange(48000)/fs
        for tone in (1000,2000,3000):
            x=np.rint((0.1+0.3*np.sin(2*np.pi*tone*t))[:,None]*np.ones((1,4))*2**23).astype(np.int32)
            report=analyze_pcm(x,tone_hz=tone)
            for channel in report['channels']:
                self.assertAlmostEqual(channel['dc_full_scale'],0.1,places=5)
                self.assertAlmostEqual(channel['ac_rms_full_scale'],0.3/math.sqrt(2),places=5)
                self.assertTrue(channel['tone_check']['pass'])
            json.dumps(report,allow_nan=False)
        self.assertFalse(analyze_pcm(x,tone_hz=1000)['channels'][0]['tone_check']['pass'])
        frequencies,amplitude=pcm_spectrum(x)
        self.assertEqual(amplitude.shape,(24001,4));self.assertEqual(frequencies[-1],24000)

    def test_silence_has_no_tone_or_delay_and_invalid_samples_rejected(self):
        zero=np.zeros((1024,4),dtype=np.int32)
        self.assertFalse(analyze_pcm(zero,tone_hz=1000)['channels'][0]['tone_check']['pass'])
        self.assertTrue(all(p['lag_samples'] is None for p in estimate_pair_delays(zero)['pairs']))
        for invalid in (np.zeros((1024,4)),np.zeros((4,4),dtype=np.int32),np.full((1024,4),8388608,dtype=np.int32)):
            with self.assertRaises(ValueError): analyze_pcm(invalid)
        with self.assertRaises(ValueError): analyze_pcm(zero,sample_rate_hz=float('nan'))

    def test_six_pair_delay_sign_and_integer_lag(self):
        rng=np.random.default_rng(45);x=rng.normal(size=8192)
        frequency=np.fft.rfftfreq(len(x),1/48000);spectrum=np.fft.rfft(x)
        spectrum[(frequency<1100)|(frequency>2900)]=0
        wave=np.fft.irfft(spectrum);wave=wave/max(abs(wave))*0.25
        shifts=[0,3,-2,1]
        samples=np.rint(np.column_stack([np.roll(wave,n) for n in shifts])*2**23).astype(np.int32)
        report=estimate_pair_delays(samples)
        self.assertEqual(len(report['pairs']),6)
        for pair in report['pairs']:
            i,j=pair['channels'];self.assertEqual(pair['lag_samples'],shifts[j]-shifts[i])
            self.assertGreater(pair['correlation'],0.99)


class PackagingChecks(unittest.TestCase):
    def test_basic_import_has_no_pynq_or_scientific_stack(self):
        subprocess.run([sys.executable,'-c',"import sys; import pynq_acoustic_camera; from pynq_acoustic_camera import AcousticArray; assert 'pynq' not in sys.modules; assert 'numpy' not in sys.modules"],cwd=ROOT,check=True)

    def test_notebook_defaults_safe_and_install_preserves_existing(self):
        from pynq_acoustic_camera import install_notebooks
        path=ROOT/'pynq_acoustic_camera/data/notebooks/04_pcm_capture.ipynb'
        notebook=json.loads(path.read_text());namespace={}
        for index,cell in enumerate(notebook['cells']):
            if cell['cell_type']=='code':
                self.assertIsNone(cell['execution_count']);self.assertEqual(cell['outputs'],[])
                exec(compile(''.join(cell['source']),str(path)+':cell'+str(index),'exec'),namespace)
        self.assertFalse(namespace['RUN_HARDWARE']);self.assertIsNone(namespace['EXPECTED_BIT_SHA256'])
        with tempfile.TemporaryDirectory() as folder:
            report=install_notebooks(folder)
            self.assertIn('04_pcm_capture.ipynb',report['installed'])
            target=Path(folder)/'04_pcm_capture.ipynb';target.write_text('user edits')
            report=install_notebooks(folder)
            self.assertIn('04_pcm_capture.ipynb',report['preserved']);self.assertEqual(target.read_text(),'user edits')


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (PcmDriverChecks,AnalysisChecks,PackagingChecks))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if len(sys.argv)==3 and sys.argv[1]=='--report':
        Path(sys.argv[2]).write_text(json.dumps(dict(scope='host_models_and_offline_analysis_only',tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),result='pass' if result.wasSuccessful() else 'fail'),indent=2)+'\n')
    raise SystemExit(not result.wasSuccessful())

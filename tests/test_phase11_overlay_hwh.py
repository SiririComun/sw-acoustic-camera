"""Host checks for the PYNQ Base Phase 11 memory-port HWH binding."""
from types import SimpleNamespace
import sys

import pytest

from pynq_acoustic_camera.continuous_capture import ContinuousPcmCapture
from pynq_acoustic_camera.pdm_control import PdmIdentityError


class FakeMMIO:
    def __init__(self, address, length):
        self.address = address
        self.length = length

    def read(self, offset):
        return 0

    def write(self, offset, value):
        pass


def overlay(ps_name, hp0_width=64, hp2_width=64):
    return SimpleNamespace(
        is_loaded=lambda: True,
        ip_dict={
            'acoustic_capture_0': {'phys_addr': 0x43C80000, 'addr_range': 65536},
            'axi_dma_audio': {
                'phys_addr': 0x40400000,
                'addr_range': 65536,
                'parameters': {
                    'c_include_sg': 1, 'c_include_mm2s': 0, 'c_include_s2mm': 1,
                    'c_sg_length_width': 23, 'c_addr_width': 32,
                    'c_m_axi_s2mm_data_width': 32,
                    'c_m_axi_s2mm_addr_width': 32,
                    'c_m_axi_sg_addr_width': 32,
                    'c_s_axis_s2mm_tdata_width': 32,
                    'c_include_s2mm_dre': 0, 'c_enable_multi_channel': 0,
                    'c_sg_include_stscntrl_strm': 0,
                },
            },
            ps_name: {'parameters': {
                'c_s_axi_hp0_data_width': hp0_width,
                'c_s_axi_hp2_data_width': hp2_width,
            }},
        },
    )


def test_phase11_accepts_base_ps_alias_and_hp2(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pynq', SimpleNamespace(MMIO=FakeMMIO, allocate=None))
    monkeypatch.setattr(ContinuousPcmCapture, '__init__', lambda self, *a, **k: None)
    capture = ContinuousPcmCapture.from_overlay(overlay('ps7_0'), memory_port='HP2')
    assert isinstance(capture, ContinuousPcmCapture)


def test_standalone_default_remains_hp0(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pynq', SimpleNamespace(MMIO=FakeMMIO, allocate=None))
    monkeypatch.setattr(ContinuousPcmCapture, '__init__', lambda self, *a, **k: None)
    capture = ContinuousPcmCapture.from_overlay(overlay('processing_system7_0'))
    assert isinstance(capture, ContinuousPcmCapture)


def test_phase11_rejects_inadequate_hp2_width(monkeypatch):
    with pytest.raises(PdmIdentityError, match='HP2 configured for 64-bit'):
        ContinuousPcmCapture.from_overlay(overlay('ps7_0', hp2_width=32),
                                          memory_port='HP2')


def test_unknown_ps_cell_is_rejected():
    with pytest.raises(PdmIdentityError, match='exactly one supported Zynq PS'):
        ContinuousPcmCapture.from_overlay(overlay('zynq_ps_0'), memory_port='HP2')

"""Acoustic camera distribution. Importing this package does not program hardware."""
from importlib import import_module

from .loader import HardwareLoader, HardwareConfigError, HardwareIntegrityError, HardwareDownloadError
from .notebooks import install_notebooks

__version__ = '0.1.0rc1'

# Scientific routines and the explicitly opted-in XADC driver are retained from
# the source project, without importing PYNQ/scientific/UI stacks on basic import.
_LAZY = {
    name: '.kinematics' for name in (
        'KinematicAnalytics', 'MultiSourceTracker', 'AcousticProfile',
        'DistanceEstimator', 'AcousticCalibrationProtocol',
        'MultipathCalibrationProtocol', 'DirectPulseCalibrationProtocol',
        'AngleOfArrivalEstimator', 'DifferentialDopplerTracker', 'TimeOfArrivalEstimator')
}
_LAZY.update({'HardwareTrigger': '.hw_trigger', 'MicrophoneArrayOverlay': '.array',
              'KinematicsDashboard': '.kinematics_dashboard'})


def __getattr__(name):
    if name not in _LAZY:
        raise AttributeError(name)
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value


__all__ = ['HardwareLoader', 'HardwareConfigError', 'HardwareIntegrityError',
           'HardwareDownloadError', 'install_notebooks'] + list(_LAZY)

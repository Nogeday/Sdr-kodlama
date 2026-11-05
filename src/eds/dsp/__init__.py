"""Signal processing core (numpy only): windows, power spectra, peaks, statistics, IQ
levels and CFAR detection.

All modules follow the conventions of docs/ARCHITECTURE.md section 2 (power-spectrum
scaling, 0 dBFS = full-scale complex exponential, fftshift-ed absolute frequency axis,
ppm sign convention).
"""

from .detect import (
    Detection,
    NoiseEstimate,
    default_n_avg,
    detect,
    effective_training_cells,
    estimate_noise,
    usable_mask,
)
from .levels import IQLevels, iq_levels
from .peaks import Peak, find_peaks, interp_peak, tone_frequency
from .psd import AVG_MODES, Spectrum, bin_frequencies, nfft_for_rbw, spectrum_from_block, welch
from .stats import (
    ca_cfar_alpha,
    cfar_alpha,
    effective_averages,
    gamma_isf,
    gamma_quantile,
    os_cfar_alpha,
    os_cfar_scale,
    os_rank_index,
    regularized_gamma_p,
    regularized_gamma_q,
)
from .windows import (
    WINDOW_NAMES,
    WindowInfo,
    bin_correlation,
    canonical_window_name,
    get_window,
    window_info,
)

__all__ = [
    # windows
    "WINDOW_NAMES", "WindowInfo", "bin_correlation", "canonical_window_name", "get_window", "window_info",
    # psd
    "AVG_MODES", "Spectrum", "bin_frequencies", "nfft_for_rbw", "spectrum_from_block", "welch",
    # peaks
    "Peak", "find_peaks", "interp_peak", "tone_frequency",
    # stats
    "ca_cfar_alpha", "cfar_alpha", "effective_averages", "gamma_isf", "gamma_quantile",
    "os_cfar_alpha", "os_cfar_scale", "os_rank_index", "regularized_gamma_p", "regularized_gamma_q",
    # levels
    "IQLevels", "iq_levels",
    # detect
    "Detection", "NoiseEstimate", "default_n_avg", "detect", "effective_training_cells",
    "estimate_noise", "usable_mask",
]

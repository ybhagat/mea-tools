import sys

import numpy as np
import pandas as pd
cimport numpy as np
cimport cython
from scipy import signal

import pymea as mea

__all__ = ['find_series_peaks', 'min_max_bin']

def find_series_peaks(series, double amp=6.0, neg_only=False):
    cdef np.ndarray[float] input_data
    cdef np.ndarray[double] bf, af, data
    cdef double pos_thresh, neg_thresh, fs, dt, t0, a, b, c, x, last_choice
    cdef int n, maxn, min_sep, lookaround, last_neg_peak
    cdef list peaks = []
    input_data = series.values

    dt = series.index[1] - series.index[0]
    fs_nyquist = (1.0/dt) / 2.0
    t0 = series.index[0]
    min_sep = int(0.001/dt)
    lookaround = int(0.0015/dt)
    last_choice = 0

    # first perform band pass filter 200Hz - 4kHz
    bf, af = signal.butter(2, (200.0/fs_nyquist, 4000.0/fs_nyquist),
                           btype='bandpass')
    data = signal.filtfilt(bf, af, input_data)
    pos_thresh = amp * np.median(np.absolute(data) / 0.6745)
    neg_thresh = -pos_thresh

    # Find points which are smaller or bigger than neighboring points
    n = 0
    last_neg_peak = 0
    maxn = len(data) - lookaround - 4
    while n < maxn:
        if data[n] < neg_thresh and data[n] < data[n-1] and data[n] < data[n+1]:
            peaks.append((fitted_peak_loc(np.arange(n-1, n+2) * dt + t0,
                                     data[n-1:n+2]),
                          data[n], neg_thresh))
            last_neg_peak = n
            n += min_sep
        elif not neg_only and (data[n] > pos_thresh and data[n] > data[n-1]
              and data[n] > data[n+1]
              and n > (last_neg_peak + lookaround)):
            # lookaround for negative peak
            for j in range(n, n+lookaround):
                if data[j] < 0.7*neg_thresh and data[j] < data[j-1] and data[j] < data[j+1]:
                    # negative peak found
                    if data[n] > 2.1*np.abs(data[j]):
                        # use positive peak if it is significanly larger
                        peaks.append((fitted_peak_loc(np.arange(n-1, n+2) * dt + t0,
                                                data[n-1:n+2]),
                                    data[n], pos_thresh))
                        last_choice = data[n]
                        n += min_sep
                    elif np.abs(data[j]) > data[n]:
                        # use negative peak if it is significanly larger
                        peaks.append((fitted_peak_loc(np.arange(j-1, j+2) * dt + t0,
                                                data[j-1:j+2]),
                                    data[j], neg_thresh))
                        last_choice = data[j]
                        last_neg_peak = j
                        n = j + min_sep
                    elif last_choice == 0:
                        # use the bigger peak
                        if data[n] > np.abs(data[j]):
                            # use positive peak if it is significanly larger
                            peaks.append((fitted_peak_loc(np.arange(n-1, n+2) * dt + t0,
                                                    data[n-1:n+2]),
                                        data[n], pos_thresh))
                            last_choice = data[n]
                            n += min_sep
                        else:
                            peaks.append((fitted_peak_loc(np.arange(j-1, j+2) * dt + t0,
                                                    data[j-1:j+2]),
                                        data[j], neg_thresh))
                            last_choice = data[j]
                            last_neg_peak = j
                            n = j + min_sep
                    elif last_choice < 0:
                        peaks.append((fitted_peak_loc(np.arange(j-1, j+2) * dt + t0,
                                                data[j-1:j+2]),
                                    data[j], neg_thresh))
                        last_choice = data[j]
                        last_neg_peak = j
                        n = j + min_sep
                    else:
                        peaks.append((fitted_peak_loc(np.arange(n-1, n+2) * dt + t0,
                                                data[n-1:n+2]),
                                    data[n], pos_thresh))
                        last_choice = data[n]
                        n += min_sep
                    break
            else:
                # no negative peak found
                peaks.append((fitted_peak_loc(np.arange(n-1, n+2) * dt + t0,
                                        data[n-1:n+2]),
                            data[n], pos_thresh))
                n += min_sep
        else:
            n += 1

    if len(peaks) < 1:
        return pd.DataFrame(columns=['time', 'amplitude', 'threshold'])

    return pd.DataFrame(np.array(peaks, dtype=np.float32),
                        columns=['time', 'amplitude', 'threshold'])


def fitted_peak_loc(x, y):
    a, b, c = np.polyfit(x, y, 2)
    x = -b/(2*a)
    return x

@cython.boundscheck(False)
cdef min_max(np.ndarray[float] d):
    if len(d) == 0:
        return 0, 0

    cdef unsigned int n
    cdef float minval = d[0]
    cdef float maxval = d[0]
    for n in range(len(d)):
        if d[n] > maxval:
            maxval = d[n]
        elif d[n] < minval:
            minval = d[n]
    return minval, maxval


def min_max_bin(np.ndarray[float] series, int bin_size, int bin_count):
    cdef unsigned int n
    cdef float minval, maxval
    cdef np.ndarray[long] edges = np.arange(0, bin_count * bin_size, bin_size)
    cdef np.ndarray[float] vals = np.empty(len(edges)*2 - 2, np.float32)

    for n in range(len(edges) - 1):
        minval, maxval = min_max(series[edges[n]:edges[n+1]])
        vals[2*n] = minval
        vals[2*n+1] = maxval

    return vals

def delay_from(np.ndarray[float] data, float t):
    # Assumes data is sorted.
    for val in data:
        if val > t:
            return val - t
    return sys.float_info.max

#!/usr/bin/env python3

import os
import time
from collections import namedtuple
import itertools

import numpy as np
import pandas as pd
from scipy import signal
import h5py
from sklearn.decomposition import PCA
from sklearn.cluster import DBSCAN

from . import util
from . import mea_cython

__all__ = ['MEARecording', 'MEASpikeDict', 'coordinates_for_electrode',
           'tag_for_electrode', 'condense_spikes', 'bandpass_filter',
           'export_spikes', 'tag_conductance_spikes', 'ConductanceSequence',
           'cofiring_events']


class MEARecording:
    """
    Main interface to an MEA recording.

    Parameters
    ----------
        store_path : str
            The path to the MEA recording, should be an HDF5 file.
    """
    def __init__(self, store_path):
        store_path = os.path.expanduser(store_path)
        if not os.path.exists(store_path):
            raise IOError('File not found.')
        self.store_path = store_path
        self.store = h5py.File(store_path)
        info = self.store[
            '/Data/Recording_0/AnalogStream/Stream_0/InfoChannel']
        self.sample_rate = 1e6/info['Tick'][0]
        self.conv = 1e-3 * info['ConversionFactor'][0]
        self.lookup = {}
        for i, l in enumerate([r[3].decode('ascii').lower() for r in info]):
            self.lookup[l] = i
        self.start_time = time.strftime(
            '%a, %d %b %Y %I:%M:%S %p',
            time.localtime(
                self.store['Data'].attrs['DateInTicks']/1e7 - 62135596800))
        self.mea = self.store['Data'].attrs['MeaName']
        self.electrode_data = self.store[
            'Data/Recording_0/AnalogStream/Stream_0/ChannelData']
        self.data_len = self.electrode_data.shape[1]
        self.duration = self.data_len / self.sample_rate
        self.spikes = []
        # Get 1st channel analog data if it exists:
        if 'Data/Recording_0/AnalogStream/Stream_1' in self.store:
            info = self.store[
                '/Data/Recording_0/AnalogStream/Stream_1/InfoChannel']
            self.analog_conv = 1e-3 * info['ConversionFactor'][0]
            self.analog_data = self.store[
                'Data/Recording_0/AnalogStream/Stream_1/ChannelData']
            self.analog_channels = [channel.decode().replace('A', 'analog')
                                    for channel in info['Label']]
        else:
            self.analog_data = None
            self.analog_channels = []

    def get(self, channels, start_time=0, end_time=None):
        """
        Returns a pandas DataFrame of the requested data.

        Parameters
        ----------
            channels : list of str or 'all'
                The requested channels, ['a4', 'b5'], or 'all' for all
                channels. Use 'analog1' to 'analog8' to access analog
                input channels.
            start_time : float
                The start time of the data requested in seconds. Defaults
                to 0.
            end_time : float
                The end time of the data requested in seconds. Defaults
                to the time of the last data point.
        """
        if channels == 'all':
            output_channels = list(self.lookup.keys())
        else:
            output_channels = [channel for channel in channels
                               if channel in list(self.lookup.keys())]
        output_channels.sort(key=lambda s: self.lookup[s])
        if end_time is None:
            end_time = self.duration

        # Find start and end indices.
        start_i = int(util.clip(start_time * self.sample_rate,
                                0, self.data_len - 1))
        end_i = int(util.clip(end_time * self.sample_rate, 0, self.data_len))
        rows = [self.lookup[channel] for channel in output_channels]
        data = (self.conv *
                self.electrode_data[rows, start_i:end_i]
                .astype(np.float32).transpose())

        # Load analog data if requested.
        if channels == 'all':
            analog_channels = self.analog_channels
        else:
            analog_channels = [channel for channel in channels
                               if channel in self.analog_channels]
        if self.analog_data is not None and len(analog_channels) > 0:
            analog_data = (self.analog_conv *
                           self.analog_data[
                               [int(channel[-1]) - 1
                                for channel in analog_channels],
                               start_i:end_i]
                           .astype(np.float32).transpose())
            data = np.concatenate((data, analog_data), axis=1)
            output_channels.extend(analog_channels)

        return pd.DataFrame(data,
                            index=np.arange(start_i, end_i)/self.sample_rate,
                            columns=output_channels, dtype=np.float32)

    def __del__(self):
        try:
            self.close()
        except (ValueError, AttributeError):
            pass

    def __getitem__(self, key):
        item = self.conv * self.electrode_data[
            self.lookup[key]].astype(np.float32)
        return pd.Series(item, index=np.arange(len(item))/self.sample_rate)

    def __len__(self):
        return len(self.lookup)

    def __str__(self):
        return (
            'File:\t\t%s\nDate:\t\t%s\nMEA:\t\t%s\nSample Rate:\t%0.1f Hz\n'
            'Duration:\t%0.2f s' %
            (self.store_path,
             self.start_time,
             self.mea.decode('ascii'),
             self.sample_rate,
             self.duration))

    def close(self):
        self.store.close()


class MEASpikeDict():
    """
    An immuatable data structure for manipulating spike data.

    Parameters
    ----------
        spike_table : DataFrame
        DataFrame as given by MEARecording.detect_spikes() or by reading
        a csv generated using pymea.export_spikes()


    MEASpikeDict acts as a dict in some situations, or a list in others. It
    maintains order and can be sorted by acting on its values.

    Examples:

    >>> spikes = MEASpikeDict(spike_table)

    >>> spikes['e8']

        electrode      time  amplitude  threshold
    158        e8   8.23285 -23.280405 -18.782986
    159        e8   8.53015 -20.926035 -18.782986
    160        e8   9.22945 -20.225355 -18.782986
    161        e8  10.17610 -19.328444 -18.782986
    162        e8  10.37980 -23.685604 -18.782986
    163        e8  10.91855 -19.250252 -18.782986
    164        e8  10.99360 -19.138470 -18.782986
    165        e8  11.18440 -20.593740 -18.782986
    166        e8  11.47700 -20.357075 -18.782986

    [9 rows x 4 columns]

    Iterate through keys:
    >>> for electrode in spikes:

    Iterate through keys and values:
    >>> for electrode, data in spikes.items()

    Sort by firing rate:
    >>> spikes.sort()
    """
    def __init__(self, spike_table):
        self.spike_table = spike_table
        self.spike_dict = {}
        self.spike_order = []
        for (tag, data) in self.spike_table.groupby('electrode'):
            self.spike_dict[tag] = data
            self.spike_order.append(tag)

    def __getitem__(self, key):
        try:
            if type(key) is int:
                return self.spike_dict[self.spike_order[key]]
            else:
                return self.spike_dict[key]
        except KeyError:
            return pd.DataFrame(columns=self.spike_table.columns)

    def __len__(self):
        return len(self.spike_order)

    def __iter__(self):
        for tag in self.spike_order:
            yield tag

    def __reversed__(self):
        for tag in reversed(self.spike_order):
            yield tag

    def items(self):
        for tag in self.spike_order:
            yield tag, self.spike_dict[tag]

    def keys(self):
        return self.spike_order

    def max_time(self):
        return self.spike_table['time'].max()

    def sort(self, key=None, reverse=True):
        """
        Sorts the electrodes by order given with key.

        key : callable
            A callable which takes a single argument, the DataFrame of a
            single electrode, and returns a value to be used for sorting.

        reverse : boolean
            If True the order is reversed.
        """
        if key is None:
            self.spike_order.sort(key=lambda e: len(self.spike_dict[e]),
                                  reverse=reverse)
        else:
            self.spike_order.sort(key=lambda e: key(self.spike_dict[e]),
                                  reverse=reverse)


def detect_spikes(analog_data, amp=6.0):
    peaks = []
    for electrode in analog_data.keys():
        p = mea_cython.find_series_peaks(analog_data[electrode], amp)
        p.insert(0, 'electrode', electrode)
        peaks.append(p)
    peaks = pd.concat(peaks, ignore_index=True)
    peaks = peaks.convert_objects(convert_numeric=True)
    #spikes = tag_conductance_spikes(peaks)
    return peaks


def sort_spikes(self, spikes, analog_data):
    for (tag, sdf) in spikes.groupby('electrode'):
        w = extract_waveforms(
            bandpass_filter(analog_data[tag]),
            self.spikes[self.spikes.electrode == tag].time.values)
        pcs = PCA(n_components=2).fit_transform(w)
        db = DBSCAN(eps=30, min_samples=3).fit(pcs)

        self.spikes.loc[sdf.electrode.index, 'electrode'] = \
            sdf.electrode.str.cat(db.labels_.astype(str), sep='.')


def export_spikes(fname, amp=6.0):
    fname = os.path.expanduser(fname)
    rec = MEARecording(fname)
    analog_data = rec.get('all')
    df = detect_spikes(analog_data, amp)
    df.to_csv(fname[:-3] + '.csv', index=False)


def extract_waveforms(series, times, window_len=0.003):
    dt = series.index[1] - series.index[0]
    span = int(window_len / 2 / dt)
    extracted = []
    for t in times:
        x = int(t / dt)
        extracted.append(series.iloc[x - span:x + span].values)
    return np.array(extracted)


def coordinates_for_electrode(tag):
    """
    Returns MEA coordinates for electrode label.

    Parameters
    ----------
        tag : str
            The electrode label, i.e. A8 or C6

    Returns
    -------
        coordinates : tuple
            A tuple of length 2 giving the x and y coordinate of
            that electrode.
    """
    tag = tag.lower()
    if tag.startswith('analog'):
        if tag.endswith('1'):
            return (0, 0)
        elif tag.endswith('2'):
            return (1, 0)
        elif tag.endswith('3'):
            return (10, 0)
        elif tag.endswith('4'):
            return (11, 0)
        elif tag.endswith('5'):
            return (0, 11)
        elif tag.endswith('6'):
            return (1, 11)
        elif tag.endswith('7'):
            return (10, 11)
        elif tag.endswith('8'):
            return (11, 11)
    cols = {'a':  0, 'b': 1, 'c': 2, 'd': 3, 'e': 4, 'f': 5, 'g': 6,
            'h': 7, 'j': 8, 'k': 9, 'l': 10, 'm': 11}
    return (cols[tag[0]], int(tag[1:]) - 1)


def tag_for_electrode(coords):
    """
    Returns MEA tag for electrode coordinates.

    Parameters
    ----------
        coords : tuple
            The electrode coordinates.

    Returns
    -------
        tag : str
            The electrode name.
    """
    lookup = {0: 'a', 1: 'b', 2: 'c', 3: 'd', 4: 'e', 5: 'f', 6: 'g', 7: 'h',
              8: 'j', 9: 'k', 10: 'l', 11: 'm'}
    tag = lookup[coords[0]] + str(coords[1])
    if tag == 'a1':
        return 'analog1'
    elif tag == 'b1':
        return 'analog2'
    elif tag == 'l1':
        return 'analog3'
    elif tag == 'm1':
        return 'analog4'
    elif tag == 'a12':
        return 'analog5'
    elif tag == 'b12':
        return 'analog6'
    elif tag == 'l12':
        return 'analog7'
    elif tag == 'm12':
        return 'analog8'
    else:
        return tag


def condense_spikes(srcdir, fname):
    """
    Condenses spikes generated with MC_Rack spike detection, 1
    channel per file, to be in the same format as detect_spikes
    gives.
    """
    with open(fname, 'a') as dest:
        dest.write('electrode,time\n')
        for f in os.listdir(srcdir):
            with open(os.path.join(srcdir, f)) as src:
                label = f[:-4].split('_')[-1]
                for line in src:
                    if len(line) > 0 and line[0].isdigit():
                        dest.write('%s,%s' % (label, line))


################################
# Conductance signal detection
################################

ConductanceSequence = namedtuple('ConductanceSequence',
                                 ['keep', 'seq', 'count'])


def cofiring_events(dataframe, min_sep=0.0005):
    """
    Parameters
    ----------
        dataframe : pandas DataFrame
            DataFrame of spike data.

        min_sep : float
            Minimum separation time between two events for
            them to be considered cofiring.

    Returns
    -------
        dataframe : pandas DataFrame
            A filtered version of dataframe which includes only events
            separated by less than min_sep.
    """
    sub_df = dataframe.sort('time')
    splits = np.concatenate([
        [0],
        np.where(np.diff(sub_df.time.values) > min_sep)[0] + 1,
        [len(sub_df)]])

    events = []
    for i in range(len(splits) - 1):
        if splits[i+1] - splits[i] == 2:
            events.append(sub_df[splits[i]:splits[i]+2])
    return events


def choose_keep_electrode(dataframe):
    """
    Chooses the electrode with the highest average amplitude among
    cofiring events between electrodes. If multiple
    electrodes have amplitudes within 20% choose the first one alphabetically.

    Parameters
    ----------
        dataframe : pandas DataFrmae
            DataFrame of spike data, should only contain cofiring events.
    Returns
    -------
        keep_electrode : str
            The name of the electrode that should be kept when removing
            conductance signals.

    """
    amplitudes = dataframe.groupby('electrode').amplitude.mean().abs()
    big_amplitudes = amplitudes[amplitudes > 0.8 * amplitudes.max()]
    return sorted(list(big_amplitudes.index))[0]


def tag_conductance_spikes(df, conductance_seqs=None):
    """
    Tags conduction spikes in dataframe.

    Parameters
    ----------
        df : pandas DataFrame
            DataFrame of spike data.
        conductance_seqs : list of ConductanceSequence
            The previously identified conduction sequences.
    """
    spikes = MEASpikeDict(df)
    tags = [tag for tag in spikes if not tag.startswith('analog')]

    conductance_locs = []

    for e1, e2 in itertools.combinations(tags, 2):
        # Find events that cofire within 1.2ms
        sub_df = pd.concat([spikes[e1], spikes[e2]])
        try:
            cofiring = pd.concat(
                [event.sort('electrode') for event
                 in cofiring_events(sub_df, 0.0012)])
            diffs = cofiring.time.diff()
        except:
            continue
        if len(cofiring) > 30 and 1000*diffs[diffs < 0.0012].std() < 0.3:
            # likely conductance sequence
            e_keep = choose_keep_electrode(cofiring)
            for i, row in cofiring.iterrows():
                if row.electrode != e_keep:
                    conductance_locs.append(i)

    df['conductance'] = False
    df.ix[conductance_locs, 'conductance'] = True
    return df


def read_binary(fname, no_channels, columns, part=(0, -1),
                fs=20000, cal=0.0610):
    """
    Loads a binary data file. Data should be 16 bits wide.

    Parameters
    ----------
    fname : str
        File name to import.
    no_channels : int
        Number of channels represented in binary data.
    fs : float
        Sampling frequency in Hz.
    """
    d = np.fromfile(fname, np.uint16)
    d = d.reshape((-1, no_channels))
    d = pd.DataFrame((d - 32768.0) * cal,
                     columns=columns,
                     index=np.arange(0, len(d)/fs, 1/fs))
    return d


def bandpass_filter(series, low=200.0, high=4000.0):
    """
    Filters given series with a 2nd order bandpass filter with default
    cutoff frequencies of 200 Hz and 4 kHz.

    Parameters
    ----------
    series : pandas.Series
        The data series to filter

    Returns
    -------
    filtered_series : pandas.Series
    """
    dt = series.index[1] - series.index[0]
    fs_nyquist = (1.0/dt) / 2.0
    if low < 0.1:
        # Lowpass filter only.
        bf, af = signal.butter(2, high/fs_nyquist, btype='lowpass')
    elif high > 10000:
        # Highpass filter only.
        bf, af = signal.butter(2, low/fs_nyquist, btype='highpass')
    else:
        bf, af = signal.butter(2, (low/fs_nyquist, high/fs_nyquist),
                               btype='bandpass')
    return pd.Series(signal.filtfilt(bf, af, series).astype(np.float32),
                     index=series.index)

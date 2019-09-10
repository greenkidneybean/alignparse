"""
===
ccs
===

Tools for inspecting output of the PacBio ``ccs`` program:
https://github.com/PacificBiosciences/unanimity/blob/develop/doc/PBCCS.md

"""


import collections
import io
import itertools
import math
import os
import re
import tempfile  # noqa: F401
import textwrap  # noqa: F401

import numpy

import pandas as pd

import pathos

import plotnine as p9

import pysam

import alignparse.utils
from alignparse.constants import CBPALETTE


class Summary:
    """Summary of a single ``ccs`` run.

    Parameters
    ----------
    name : str
        Name of the ``ccs`` run.
    fastqfile : str
        FASTQ file with circular consensus sequences, typically generated by
        PacBio ``ccs`` program. Can be gzipped. Number of passes is determined
        from the ``np`` tag in the FASTQ comments if present.
    reportfile : str or None
        Report file generated by ``ccs`` using ``--reportFile`` flag, or
        None if no such report available.

    Attributes
    ----------
    name : str
        Name of ``ccs`` run.
    fastqfile : str
        FASTQ file with the circular consensus sequences.
    reportfile : str or None
        ``ccs`` report file. Currently handles reports in formats generated
        by ``ccs`` versions 3.* and 4.0.
    zmw_stats : pandas.DataFrame or None
        Stats on ZMW extracted from `reportfile`.
    passes : numpy.array or None
        Lists number of passes for all circular consensus sequences, or
        `None` if this information is not available in a ``np`` tag
        in `fastqfile`.
    accuracy : numpy.array
        Lists accuracies for all circular consensus sequences. This is the
        average accuracy across sites computed from Q-values in `fastqfile`.
    length : numpy.array
        Lists lengths for all circular consensus sequences.

    """

    def __init__(self, name, fastqfile, reportfile):
        """See main class docstring."""
        self.name = name
        self.fastqfile = fastqfile
        if not os.path.isfile(fastqfile):
            raise IOError(f"cannot find `fastqfile` {fastqfile}")

        ccs_stats = get_ccs_stats(self.fastqfile)
        self.passes = ccs_stats.passes
        self.accuracy = ccs_stats.accuracy
        self.length = ccs_stats.length
        assert len(self.accuracy) == len(self.length)
        assert (self.passes is None) or (len(self.passes) == len(self.length))

        if reportfile:
            self.reportfile = reportfile
            if not os.path.isfile(reportfile):
                raise IOError(f"cannot find `reportfile` {reportfile}")
            self.zmw_stats = report_to_stats(self.reportfile)
            zmw_stats_nccs = (self.zmw_stats
                              .query('status.str.match("^Success")')
                              ['number']
                              .sum()
                              )
            if len(self.length) != zmw_stats_nccs:
                raise ValueError('`fastqfile`, `reportfile` differ on number '
                                 f"CCSs.\n{fastqfile} has {len(self.passes)}\n"
                                 f"{reportfile} has {zmw_stats_nccs}")

        else:
            self.zmw_stats = None
            self.reportfile = None


class Summaries:
    """Summaries of ``ccs`` runs.

    Parameters
    ----------
    df : pandas.DataFrame
        Data frame giving information on runs of ``ccs`` being summarized.
    name_col : str
        Column in `df` with name of ``ccs`` run.
    fastq_col : str
        Column in `df` with FASTQ file for run, appropriate for passing
        to :class:`Summary` as `fastqfile`.
    report_col : str or None
        Column in `df` with report for run, appropriate for passing to
        :class:`Summary` as `reportfile`. Set to `None` if no reports.
        If there are no reports, then ZMW stats are not available.
    ncpus : int
        Number of CPUs to use; -1 means all available. Useful as processing
        all the FASTQ files to compute read accuracies can take a while.

    Attributes
    ----------
    summaries : list
        List of :class:`Summary` objects for each run.

    """

    def __init__(self, df, *,
                 name_col='name', fastq_col='fastq', report_col='report',
                 ncpus=-1):
        """See main class docstring."""
        cols = [name_col, fastq_col]
        if report_col:
            cols.append(report_col)
        if len(cols) != len(set(cols)):
            raise ValueError(f"repeated column names in `df`")
        for col in cols:
            if col not in df.columns:
                raise ValueError(f"`df` lacks column {col}")

        if len(df[name_col]) != len(df[name_col].unique()):
            raise ValueError(f"the run names in {name_col} are not unique")

        # get Summary for each run in a multiprocessing pool
        if ncpus == -1:
            ncpus = pathos.multiprocessing.cpu_count()
        else:
            ncpus = min(pathos.multiprocessing.cpu_count(), ncpus)
        if ncpus < 1:
            raise ValueError('`ncpus` must be >= 1')
        elif ncpus > 1:
            pool = pathos.pools.ProcessPool(ncpus)
            map_func = pool.map
        else:
            def map_func(f, *args):
                return [f(*argtup) for argtup in zip(*args)]
        self.summaries = map_func(Summary,
                                  df[name_col],
                                  df[fastq_col],
                                  (df[report_col] if report_col else
                                   itertools.repeat(None)),
                                  )
        # close, clear pool: https://github.com/uqfoundation/pathos/issues/111
        if ncpus > 1:
            pool.close()
            pool.join()
            pool.clear()

    def plot_ccs_stats(self, variable, *,
                       trim_frac=0.005, bins=25, histogram_stat='count',
                       maxcol=None, panelsize=1.75):
        """Plot histograms of CCS stats for all runs.

        Parameters
        ----------
        variable : {'length', 'passes', 'accuracy'}
            Variable for which we plot stats. You will get an error
            if :meth:`Summaries.has_stat` is not true for `variable`.
        trim_frac : float
            Trim this amount of the bottom and top fraction from the
            data before plotting. Useful if outliers greatly extend scale.
        bins : int
            Number of histogram binds
        histogram_stat : {'count', 'density'}
            Plot the count of CCSs or their density normalized for each run.
        maxcol : None or int
            Max number of columns in faceted plot.
        panelsize : float
            Size of each plot panel.

        Returns
        -------
        plotnine.ggplot.ggplot
            A panel of histograms.

        """
        df = (self.ccs_stats(variable)
              .assign(lower=lambda x: x[variable].quantile(trim_frac),
                      upper=lambda x: x[variable].quantile(1 - trim_frac),
                      trim=lambda x: ((x[variable] > x['upper']) |
                                      (x[variable] < x['lower']))
                      )
              .query('not trim')
              )

        npanels = len(df['name'].unique())
        if maxcol is None:
            ncol = npanels
        else:
            ncol = min(maxcol, npanels)
        nrow = math.ceil(npanels / ncol)

        p = (p9.ggplot(df, p9.aes(variable, y=f"..{histogram_stat}..")) +
             p9.geom_histogram(bins=bins) +
             p9.facet_wrap('~ name', ncol=ncol) +
             p9.theme(figure_size=(panelsize * ncol, panelsize * nrow),
                      axis_text_x=p9.element_text(angle=90,
                                                  vjust=1,
                                                  hjust=0.5)
                      ) +
             p9.ylab('number of CCSs')
             )

        return p

    def ccs_stats(self, variable):
        """Get CCS stats for all runs.

        Parameters
        ----------
        variable : {'length', 'passes', 'accuracy'}
            Variable for which we get stats. You will get an error
            if :meth:`Summaries.has_stat` is not true for `variable`.

        Returns
        -------
        pandas.DataFrame
            Data frame with columns of 'name' (holding run name) and the value
            of `variable` giving variable value for all CCSs.

        """
        if not self.has_stat(variable):
            raise ValueError(f"no stats for `variable` {variable}")

        df_list = []
        for summary in self.summaries:
            df_list.append(pd.DataFrame({'name': summary.name,
                                         variable: getattr(summary, variable)
                                         }))

        return (pd.concat(df_list, sort=False, ignore_index=True)
                .assign(name=lambda x: pd.Categorical(x['name'],
                                                      x['name'].unique(),
                                                      ordered=True))
                )

    def has_stat(self, variable):
        """Do the summaries contain a statistic?

        Parameters
        ----------
        variable : {'length', 'passes', 'accuracy'}
            Variable for which we want statistics.

        Returns
        -------
        bool
            `True` if all runs have statistics for `variable`,
            `False` otherwise.

        """
        valid_variables = {'length', 'passes', 'accuracy'}
        if variable not in valid_variables:
            raise ValueError(f"Invalid `variable` {variable}. Must be one "
                             f"of: {valid_variables}")
        return all(getattr(summary, variable) is not None for
                   summary in self.summaries)

    def plot_zmw_stats(self, **kwargs):
        """Plot of ZMW stats for all runs.

        Note
        ----
        Raises an error if :meth:`Summaries.has_zmw_stats` is not `True`.

        Parameters
        ----------
        ``**kwargs`` : dict
            Keyword arguments passed to :meth:`Summaries.zmw_stats`.

        Returns
        -------
        plotnine.ggplot.ggplot
            Stacked bar graph of ZMW stats for each run.

        """
        df = self.zmw_stats(**kwargs)

        p = (p9.ggplot(df, p9.aes(x='name', y='number', fill='status')) +
             p9.geom_col(position=p9.position_stack(reverse=True), width=0.8) +
             p9.theme(axis_text_x=p9.element_text(angle=90,
                                                  vjust=1,
                                                  hjust=0.5),
                      figure_size=(0.4 * len(df['name'].unique()), 2.5)
                      ) +
             p9.ylab('number of ZMWs') +
             p9.xlab('')
             )

        if len(df['status'].unique()) < len(CBPALETTE):
            p = p + p9.scale_fill_manual(CBPALETTE[1:])

        return p

    def zmw_stats(self, *, minfailfrac=0.01, groupsuccess=True):
        """Get ZMW stats for all runs.

        Note
        ----
        Raises an error if :meth:`Summaries.has_zmw_stats` is not `True`.

        Parameters
        ----------
        minfailfrac : float
            Group failure categories with <= this fraction ZMWs for all runs.
        groupsuccess : bool
            Group all success categories into 'Success -- CCS generated'.

        Returns
        -------
        pandas.DataFrame
            Data frame with stats on ZMW status for all runs.

        """
        if not self.has_zmw_stats():
            raise ValueError('ZMW stats not available')

        df = (pd.concat([summary.zmw_stats.assign(name=summary.name)
                         for summary in self.summaries],
                        sort=False, ignore_index=True)
              [['name', 'status', 'number', 'fraction']]
              .assign(max_fraction=lambda x: (x.groupby('status')
                                              ['fraction']
                                              .transform(max)
                                              ),
                      failed=lambda x: x['status'].str.contains('Failed'),
                      other=lambda x: ((x['max_fraction'] < minfailfrac) &
                                       x['failed'])
                      )
              )

        other_df = (df.query('other')
                    .groupby('name')
                    .aggregate({'number': sum, 'fraction': sum, 'failed': any})
                    .assign(status='Failed -- Other reason')
                    .reset_index()
                    .assign(max_fraction=lambda x: (x.groupby('status')
                                                    ['fraction']
                                                    .transform(max)
                                                    ))
                    )

        df = (df
              .query('not other')
              .drop(columns='other')
              .merge(other_df, how='outer')
              )

        if groupsuccess:
            success_df = (df
                          .query('status.str.match("^Success")')
                          .groupby('name')
                          .aggregate({'number': sum, 'fraction': sum,
                                      'failed': any, 'max_fraction': max})
                          .reset_index()
                          .assign(status='Success -- CCS generated')
                          )
            df = (df
                  .query('not status.str.match("^Success")')
                  .merge(success_df, how='outer')
                  )

        # order columns
        names = [summary.name for summary in self.summaries]
        statuses = (df
                    .sort_values(['failed', 'max_fraction'],
                                 ascending=[True, False])
                    ['status']
                    .unique()
                    )
        df = (df
              .assign(name=lambda x: pd.Categorical(x['name'],
                                                    names,
                                                    ordered=True),
                      status=lambda x: pd.Categorical(x['status'],
                                                      statuses,
                                                      ordered=True),
                      )
              .sort_values(['name', 'status'])
              .drop(columns=['max_fraction', 'failed'])
              .reset_index(drop=True)
              )

        return df

    def has_zmw_stats(self):
        """Are ZMW stats available?

        Returns
        -------
        bool
            `True` if all runs have ZMW stats, `False` otherwise.

        """
        return all(summary.zmw_stats is not None for
                   summary in self.summaries)


def report_to_stats(reportfile):
    """Parse ZMW statistics from report file.

    Parameters
    ----------
    reportfile : str
        Report file generatedy by ``ccs`` using ``--reportFile`` flag.
        Handles formats generated ``ccs`` version 3.* and 4.0.

    Returns
    -------
    pandas.DataFrame
        A data frame with the statistics.

    Example
    -------
    An example of the ``ccs`` version 4.0.0 output:

    >>> reportfile = tempfile.NamedTemporaryFile(mode='w')
    >>> _ = reportfile.write(textwrap.dedent('''
    ...     ZMWs input          (A)  : 686919
    ...     ZMWs generating CCS (B)  : 182500 (26.57%)
    ...     ZMWs filtered       (C)  : 504419 (73.43%)
    ...
    ...     Exclusive ZMW counts for (C):
    ...     No usable subreads       : 0 (0.00%)
    ...     Below SNR threshold      : 0 (0.00%)
    ...     Lacking full passes      : 344375 (68.27%)
    ...     Heteroduplexes           : 1056 (0.21%)
    ...     Min coverage violation   : 2035 (0.40%)
    ...     Draft generation error   : 7636 (1.51%)
    ...     Draft above --max-length : 49 (0.01%)
    ...     Draft below --min-length : 2 (0.00%)
    ...     Lacking usable subreads  : 0 (0.00%)
    ...     CCS did not converge     : 0 (0.00%)
    ...     CCS below minimum RQ     : 149315 (29.60%)
    ...     Unknown error            : 0 (0.00%)
    ...     ''').lstrip())
    >>> reportfile.flush()
    >>> report_to_stats(reportfile.name)
                          status  number  fraction
    0        ZMWs generating CCS  182500  0.265660
    1         No usable subreads       0  0.000000
    2        Below SNR threshold       0  0.000000
    3        Lacking full passes  344375  0.501297
    4             Heteroduplexes    1056  0.001537
    5     Min coverage violation    2035  0.002962
    6     Draft generation error    7636  0.011116
    7   Draft above --max-length      49  0.000071
    8   Draft below --min-length       2  0.000003
    9    Lacking usable subreads       0  0.000000
    10      CCS did not converge       0  0.000000
    11      CCS below minimum RQ  149315  0.217354
    12             Unknown error       0  0.000000
    >>> reportfile.close()

    An example of the ``ccs`` version 3.1.0 output:

    >>> reportfile = tempfile.NamedTemporaryFile(mode='w')
    >>> _ = reportfile.write(textwrap.dedent('''
    ...     ZMW Yield
    ...     Success -- CCS generated,242220,45.57%
    ...     Failed -- Below SNR threshold,0,0.00%
    ...     Failed -- No usable subreads,4877,0.92%
    ...     Failed -- Insert size too long,35,0.00%
    ...     Failed -- Insert size too small,0,0.00%
    ...     Failed -- Not enough full passes,180620,33.98%
    ...     Failed -- Too many unusable subreads,1,0.00%
    ...     Failed -- CCS did not converge,23,0.00%
    ...     Failed -- CCS below minimum predicted accuracy,103801,19.53%
    ...     Failed -- Unknown error during processing,0,0.00%
    ...
    ...
    ...     Subread Yield
    ...     Success - Used for CCS,10972010,89.06%
    ...     Failed -- Below SNR threshold,0,0.00%
    ...     Failed -- Alpha/Beta mismatch,171,0.00%
    ...     Failed -- Below minimum quality,0,0.00%
    ...     Failed -- Filtered by size,144209,1.17%
    ...     Failed -- Identity too low,2745750,22.29%
    ...     Failed -- Z-Score too low,0,0.00%
    ...     Failed -- From ZMW with too few passes,274296,2.23%
    ...     Failed -- Other,928871,7.54%
    ...     ''').lstrip())
    >>> reportfile.flush()
    >>> report_to_stats(reportfile.name)
                                               status  number  fraction
    0                        Success -- CCS generated  242220    0.4557
    1                   Failed -- Below SNR threshold       0    0.0000
    2                    Failed -- No usable subreads    4877    0.0092
    3                  Failed -- Insert size too long      35    0.0000
    4                 Failed -- Insert size too small       0    0.0000
    5                Failed -- Not enough full passes  180620    0.3398
    6            Failed -- Too many unusable subreads       1    0.0000
    7                  Failed -- CCS did not converge      23    0.0000
    8  Failed -- CCS below minimum predicted accuracy  103801    0.1953
    9       Failed -- Unknown error during processing       0    0.0000
    >>> reportfile.close()

    An example of the ``ccs`` version 3.4.1 output:

    >>> reportfile = tempfile.NamedTemporaryFile(mode='w')
    >>> _ = reportfile.write(textwrap.dedent('''
    ...     ZMW Yield
    ...     Success (without retry) -- CCS generated,202033,29.44%
    ...     Success (with retry)    -- CCS generated,2,0.00%
    ...     Failed -- Below SNR threshold,0,0.00%
    ...     Failed -- No usable subreads,2093,0.31%
    ...     Failed -- Insert size too long,10,0.01%
    ...     Failed -- Insert size too small,79,0.01%
    ...     Failed -- Not enough full passes,343876,50.12%
    ...     Failed -- Too many unusable subreads,0,0.00%
    ...     Failed -- CCS did not converge,0,0.00%
    ...     Failed -- CCS below minimum predicted accuracy,138083,20.12%
    ...     Failed -- Unknown error during processing,0,0.00%
    ...
    ...
    ...     ''').lstrip())
    >>> reportfile.flush()
    >>> report_to_stats(reportfile.name)  # doctest: +NORMALIZE_WHITESPACE
                                               status  number fraction
    0        Success (without retry) -- CCS generated  202033   0.2944
    1        Success (with retry)    -- CCS generated       2   0.0000
    2                   Failed -- Below SNR threshold       0   0.0000
    3                    Failed -- No usable subreads    2093   0.0031
    4                  Failed -- Insert size too long      10   0.0001
    5                 Failed -- Insert size too small      79   0.0001
    6                Failed -- Not enough full passes  343876   0.5012
    7            Failed -- Too many unusable subreads       0   0.0000
    8                  Failed -- CCS did not converge       0   0.0000
    9  Failed -- CCS below minimum predicted accuracy  138083   0.2012
    10      Failed -- Unknown error during processing       0   0.0000
    >>> reportfile.close()

    """
    for func in [_report_to_stats_v4, _report_to_stats_v3]:
        df = func(reportfile)
        if df is not None:
            return df

    raise IOError(f"Cannot match report in {reportfile}")


def _report_to_stats_v4(reportfile):
    """Implement :func:`report_to_stats` for ``ccs`` 4.* output.

    Returns
    -------
    pandas.DataFrame or None
        Returns None if cannot match reportfile.

    """
    reportmatch = re.compile(
            r'^ZMWs input\s+\(A\)\s+:\s+\d+\n'
            r'ZMWs generating CCS\s+\(B\)\s+:\s+(?P<n_generated>\d+) \S+\n'
            r'ZMWs filtered\s+\(C\)\s+:\s+\d+ \S+\n'
            '\n'
            r'Exclusive ZMW counts for \(C\):\n'
            r'(?P<failed_text>([\w \-]+: \d+ \S+\n)+)$'
            )
    with open(reportfile) as f:
        report = f.read()
    m = reportmatch.search(report)
    if m is not None:
        failed_records = [(line.split(':')[0].strip(),
                           int(line.split(':')[1].split()[0]))
                          for line in m.group('failed_text').split('\n')
                          if line]
        return (pd.DataFrame({'status': ['ZMWs generating CCS'],
                              'number': [int(m.group('n_generated'))]
                              })
                .append(pd.DataFrame(failed_records,
                                     columns=['status', 'number'])
                        )
                .reset_index(drop=True)
                .assign(fraction=lambda x: x['number'] / x['number'].sum())
                )
    else:
        return None


def _report_to_stats_v3(reportfile, *, stat_type='zmw'):
    """Implement :func:`report_to_stats` for ``ccs`` 3.* output.

    Returns
    -------
    pandas.DataFrame or None
        Returns None if cannot match reportfile.

    """
    reportmatch = re.compile('^ZMW Yield\n(?P<zmw>(.+\n)+)\n\n'
                             '(?:Subread Yield\n(?P<subread>(.+\n)+))?$')
    with open(reportfile) as f:
        report = f.read()
    m = reportmatch.search(report)
    if m:
        return (pd.read_csv(io.StringIO(m.group(stat_type)),
                            names=['status', 'number', 'percent'])
                .assign(fraction=lambda x: (x.percent.str.slice(None, -1)
                                            .astype(float) / 100))
                [['status', 'number', 'fraction']]
                )
    else:
        return None


def get_ccs_stats(fastqfile, *, pass_tag='np'):
    """Get basic statistics about circular consensus sequences.

    Parameters
    ----------
    fastqfile : str
        FASTQ file with circular consensus sequences, generated from BAM output
        of ``ccs``.
    pass_tag : str
        Tag in FASTQ file header giving number of passes (if present).

    Returns
    -------
    collections.namedtuple
        The 3-tuple `(passes, accuracy, length)` contains numpy arrays
        giving number of passes, accuracies, and lengths for sequences in
        `fastqfile`. The accuracy (average across sequence) and length
        are computed from the quality scores and sequence in `fastqfile`;
        the number of passes must be provided as `pass_tag` and are returned
        as `None` if that tag is missing from any read in `fastqfile`.

    Example
    -------
    >>> fastqfile = tempfile.NamedTemporaryFile(mode='w')
    >>> _ = fastqfile.write(textwrap.dedent('''
    ...   @m54228_190118_102822/4194373/ccs np:i:18
    ...   GGTACCACACTCTTTCCCTACACGACGCTCTGCCGATCTCGGCCATTACGTGTTTTATCTA
    ...   +
    ...   ~~~~{~~~~~~~c~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~i~~~~~~~(
    ...   @m54228_190118_102822/4194374/ccs np:i:51
    ...   GCACGGCGTCACACTTTGCTATGCCATAGCATGTTTATCCATAAGATTAGCGGATCCTACCT
    ...   +
    ...   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ...   ''').lstrip())
    >>> fastqfile.flush()
    >>> get_ccs_stats(fastqfile.name)  # doctest: +NORMALIZE_WHITESPACE
    CCS_Stats(passes=array([18, 51]),
              accuracy=array([0.99672907, 1.        ]),
              length=array([61, 62]))
    >>> fastqfile.close()

    Similar test without ``np`` tag in FASTQ file:

    >>> fastqfile = tempfile.NamedTemporaryFile(mode='w')
    >>> _ = fastqfile.write(textwrap.dedent('''
    ...   @m54228_190118_102822/4194373/ccs
    ...   GGTACCACACTCTTTCCCTACACGACGCTCTGCCGATCTCGGCCATTACGTGTTTTATCTA
    ...   +
    ...   ~~~~{~~~~~~~c~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~i~~~~~~~(
    ...   @m54228_190118_102822/4194374/ccs
    ...   GCACGGCGTCACACTTTGCTATGCCATAGCATGTTTATCCATAAGATTAGCGGATCCTACCT
    ...   +
    ...   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ...   ''').lstrip())
    >>> fastqfile.flush()
    >>> get_ccs_stats(fastqfile.name)  # doctest: +NORMALIZE_WHITESPACE
    CCS_Stats(passes=None,
              accuracy=array([0.99672907, 1.        ]),
              length=array([61, 62]))
    >>> fastqfile.close()

    """
    if pass_tag:
        passmatch = re.compile(r'(?:^|\s)'  # start of str or space
                               rf"{pass_tag}:i:(?P<pass>\d+)"
                               r'(?:\s|$)'  # end of str or space
                               )
        passes = []
    else:
        passes = None

    length = []
    accuracy = []
    for rec in pysam.FastxFile(fastqfile):
        length.append(len(rec.sequence))
        accuracy.append(alignparse.utils.qvals_to_accuracy(
                            numpy.array(rec.get_quality_array())))
        if passes is not None:
            if not rec.comment:
                passes = None
            else:
                m = passmatch.search(rec.comment)
                if not m:
                    passes = None
                else:
                    passes.append(m.group('pass'))

    if passes is not None:
        passes = numpy.array(passes, dtype='int')
    CCS_Stats = collections.namedtuple('CCS_Stats', 'passes accuracy length')
    return CCS_Stats(passes=passes,
                     accuracy=numpy.array(accuracy, dtype='float'),
                     length=numpy.array(length, dtype='int'),
                     )


if __name__ == '__main__':
    import doctest
    doctest.testmod()

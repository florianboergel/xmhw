#!/usr/bin/env
# coding: utf-8
# Copyright 2020 ARC Centre of Excellence for Climate Extremes
# author: Paola Petrelli <paola.petrelli@utas.edu.au>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import xarray as xr
import numpy as np
import pandas as pd
import dask
from .identify import (
    land_check,
    add_doy,
    get_calendar,
    define_events,
    runavg,
    window_roll,
    calculate_thresh,
    calculate_seas,
    annotate_ds,
)
from .features import flip_cold
from .exception import XmhwException


def threshold(
    temp,
    tdim = "time",
    climatologyPeriod = [None, None],
    pctile = 90,
    windowHalfWidth = 5,
    smoothPercentile = True,
    smoothPercentileWidth = 31,
    maxPadLength = None,
    coldSpells = False,
    tstep = False,
    anynans = False,
    skipna = False,
):
    """Calculate threshold and mean climatology (day-of-year).

    Parameters
    ----------
    temp: xarray DataArray
        Temperature timeseries array
    tdim: str, optional
        Name of time dimension (default 'time')
    climatologyPeriod: list(int), optional
        Period over which climatology is calculated, specified as list
        of start and end years. Default is to use the full time series.
    pctile: int, optional
        Threshold percentile used to detect events (default=90)
    windowHalfWidth: int, optional
        Half width of window about day-of-year used for the pooling of
        values and calculation of threshold percentile (default=5)
    smoothPercentile: bool, optional
        If True smooth the threshold percentile timeseries with a
        moving average (default is True)
    smoothPercentileWidth: int, optional
        Width of moving average window for smoothing threshold in days,
        has to be odd number (default=31)
    maxPadLength: int, optional
        Specifies the maximum length (days) over which to interpolate
        NaNs in input temp time series. i.e., any consecutive blocks of
        NaNs with length greater than maxPadLength will be left as
        NaN. If None it does not interpolate (default is None).
    coldSpells: bool, optional
        If True the code detects cold events instead of heat events
        (default is False)
    tstep: bool, optional
        If True the timeseries timestep is used as base for 'doy' unit
        To use with any but 365/366 days year daily files
        (default is False)
    anynans: bool, optional
        Defines in land_check which cells will be dropped, if False
        only ones with all NaNs values, if True all cells with even
        1 NaN along time dimension will be dropped (default is False)
    skipna: bool, optional
        If True percentile and mean function will use skipna=True.
        Using skipna option is much slower (default is False)

    Returns
    -------
    clim : xarray Dataset
        includes thresh climatological threshold
                 seas   climatological mean
    """

    # Check smooth percentile window width is odd
    # and that time dimension (tdim) is present
    if smoothPercentileWidth % 2 == 0:
        raise XmhwException("smoothPercentileWidth should be odd")
    if tdim not in temp.dims:
        raise XmhwException(
            f"{tdim} dimension not present, default"
            + "is 'time' or pass as tdim='time_dimension_name'"
        )

    # Set climatology period, if unset use full range of available data
    if all(climatologyPeriod):
        tslice = {
            tdim: slice(
                f"{climatologyPeriod[0]}-01-01",
                f"{climatologyPeriod[1]}-12-31"
            )
        }
        temp = temp.sel(**tslice)
    # Check if there is only one dimension (assumed as time)
    # then skip all multidimensional operations
    dims = list(temp.dims)
    if len(dims) == 1:
        point = True
    # Save original attributes in dictionary to assign to final dataset
    ds_attrs = {}
    ds_attrs["ts"] = temp.attrs
    # ds_attrs[tdim+'encoding'] = temp.encoding
    for c in temp.dims:
        ds_attrs[c] = temp[c].attrs
    # Returns an array stacked on all dimensions excluded time
    # Land cells are removed and new dimensions are (time,cell)
    if point:
        ts = temp
    else:
        ts = land_check(temp, tdim=tdim, anynans=anynans)

    # check if the calendar attribute is present in time dimension
    # if not try to guess length of year
    year_days = get_calendar(ts[tdim])
    if year_days == 360.0:
        tstep = True
    ts = add_doy(ts, tdim=tdim, keep_tstep=tstep)
    # else:
    #    XMHW.Exception("Module is not yet set to work with a calendar "
    #        + "different from gregorian, standard, proleptic_gregorian."
    #        + "NB We treat all these calendars in the same way in the "
    #        + "assumption that the timeseries starts after 1582")

    # Flip ts time series if detecting cold spells
    if coldSpells:
        ts = -1.0 * ts

    # Linear interpolation of all consecutive missing blocks
    # of length <= maxPadLength
    # NB by default maxPadLength is None and there is no interpolation
    if maxPadLength:
        ts = ts.interpolate_na(dim=tdim, max_gap=maxPadLength)

    # Open list for partial results and dataset to save calculated results
    climls = []
    ds = xr.Dataset()

    # if timeseries is a single point we skipped grid operations
    if point:
        climls.append(
            calc_clim(
                ts,
                tdim,
                pctile,
                windowHalfWidth,
                smoothPercentile,
                smoothPercentileWidth,
                tstep,
                skipna,
                )
            )

    else:
    # Loop over each cell to calculate climatologies, main functions
    # are delayed, so loop is automatically run in parallel
        for c in ts.cell:
            climls.append(
                calc_clim(
                    ts.sel(cell=c),
                    tdim,
                    pctile,
                    windowHalfWidth,
                    smoothPercentile,
                    smoothPercentileWidth,
                    tstep,
                    skipna,
                )
            )
    results = dask.compute(climls)

    #thresh_results = [r[0] for r in results[0]]
    # apply temporary fix suggested by @bjnmr issue #49
    # as newver version of xarray are removing coords when calculating quantile but not for mean
    # as I removed the multiindex I'm passing directly r[1].coords and not r[1]['cell'].coords 
    # this causes issues when trying to concatenate
    thresh_results = [r[0].assign_coords(r[1].coords) for r in results[0]]
    seas_results = [r[1] for r in results[0]]
    if point:
        ds["thresh"] = thresh_results[0]
        ds["seas"] = seas_results[0]
    else:
        ds["thresh"] = xr.concat(thresh_results, dim='cell')
        ds["seas"] = xr.concat(seas_results, dim='cell')
        dims = [k for k in ts.cell.coords.keys()]
        ds = ds.set_xindex(dims)
        ds = ds.unstack(dim='cell')
    ds.thresh.name = "threshold"
    ds.seas.name = "seasonal"

    # add previously saved attributes to ds
    ds = annotate_ds(ds, ds_attrs, "clim")
    # add all parameters used to global attributes
    dum = [ts[tdim][0].dt.year.values, ts[tdim][-1].dt.year.values]
    params = f"""Threshold calculated using:
    {pctile} percentile;
    climatology period is {dum[0]}-{dum[1]}';
    window half width used for percentile is {windowHalfWidth}"""
    if skipna:
        params = (
            params
            + """;
            NaNs where skipped in percentile and mean calculations"""
        )
    if smoothPercentile:
        params = (
            params
            + f""";
         width of moving average window to smooth percentile is
         {smoothPercentileWidth}"""
        )
    if anynans:
        params = (
            params
            + """;
            any grid point with even only 1 NaN along time
            axis has been removed from calculation"""
        )
    ds.attrs["xmhw_parameters"] = params
    return ds


def calc_clim(
    ts,
    tdim,
    pctile,
    windowHalfWidth,
    smoothPercentile,
    smoothPercentileWidth,
    tstep,
    skipna,
):
    """Calculate climatologies.

    Parameters
    ----------
    ts: xarray DataArray
        Temperature timeseries array
    tdim: str
        Name of time dimension
    pctile: int
        Threshold percentile used to detect events
    windowHalfWidth: int
        Half width of window about day-of-year used for the pooling of
        values and calculation of threshold percentile
    smoothPercentile: bool
        If True smooth the threshold percentile timeseries with a
        moving average
    smoothPercentileWidth: int
        Width of moving average window for smoothing threshold in days,
        has to be odd number
    tstep: bool
        If True the timeseries timestep is used as base for 'doy' unit
        To use with any but 365/366 days year daily files
    skipna: bool
        If True percentile and mean function will use skipna=True.
        Using skipna option is much slower

    Returns
    -------
    thresh_climYear: xarray DataArray
        Climatological threshold for the grid cell
    seas_climYear: xarray DataArray
        Climatological mean for the grid cell
    """

    twindow = window_roll(ts, windowHalfWidth, tdim)
    # Rechunk twindow so all timeseries is in 1 chunk
    twindow = twindow.chunk({"z": -1})

    # Calculate threshold and seasonal climatology across years
    thresh_climYear = calculate_thresh(twindow, pctile, skipna, tstep)
    seas_climYear = calculate_seas(twindow, skipna, tstep)

    # If smooth option on smooth both climatologies
    if smoothPercentile:
        thresh_climYear = runavg(thresh_climYear, smoothPercentileWidth)
        seas_climYear = runavg(seas_climYear, smoothPercentileWidth)

    return thresh_climYear, seas_climYear


def detect(
    temp,
    th,
    se,
    tdim = "time",
    minDuration = 5,
    joinGaps = True,
    maxGap = 2,
    maxPadLength = None,
    coldSpells = False,
    intermediate = False,
    anynans = False,
    tstep = False,
):
    """Applies the Hobday et al. (2016) marine heat wave definition to
    a temperature timeseries. Returns properties of all detected MHWs.

    Parameters
    ----------
    temp: xarray DataArray
        Temperature timeseries array
    th: xarray DataArray
        Climatological threshold (e.g., 90th percentile)
    se: xarray DataArray
        Climatological mean
    tdim: str, optional
        Name of time dimension (default='time')
    minDuration: int, optional
        Minimum duration (days) to accept detected MHWs (default=5)
    joinGaps: bool, optional
       If True join MHWs separated by a short gap (default is True)
    maxGap: int, optional
        Maximum limit of gap length (days) between events (default=2)
    maxPadLength: int, optional
        Specifies the maximum length (days) over which to interpolate
        NaNs in input temp time series. i.e., any consecutive blocks of
        NaNs with length greater than maxPadLength will be left as
        NaN. If None it does not interpolate (default is None).
    coldSpells: bool, optional
        If True the code detects cold events instead of heat events
        (default is False)
    intermediate: bool, optional
        If True return also dataset with input data, detected events
        and some events properties along time axis (default is False)
    anynans: bool, optional
        Defines in land_check which cells will be dropped, if False
        only ones with all NaNs values, if True all cells with even
        1 NaN along time dimension will be dropped (default is False)
    tstep: bool, optional
        If True the timeseries timestep is used as base for 'doy' unit
        To use with any but 365/366 days year daily files
        (default is False)

    Returns
    -------
    mhw: xarray Dataset
        Detected marine heat waves (MHWs). Has new 'events' dimension
    mhw_inter: xarray Dataset, optional
        Dataset with input data, detected events and some events
        properties along time axis. If intermediate is False is None
    """

    # check maxGap < minDuration
    if maxGap >= minDuration:
        raise XmhwException(
            "Maximum gap between mhw events should"
            + " be smaller than event minimum duration"
        )
    # Check if there is only one dimension (assumed as time)
    # then skip all multidimensional operations
    dims = list(temp.dims)
    if len(dims) == 1:
        point = True
    # if time dimension different from time, rename it
    #temp = temp.rename({tdim: "time"})
    # save original attributes in a dictionary to assign to final dataset
    ds_attrs = {}
    ds_attrs["ts"] = temp.attrs
    # ds_attrs[tdim+'encoding'] = temp.encoding
    for c in temp.coords:
        ds_attrs[c] = temp[c].attrs

    # Returns an array stacked on all dimensions excluded time, doy
    # Land cells are removed and new dimensions are (time,cell)
    if point:
        ts = temp
    else:
        ts = land_check(temp, tdim=tdim, anynans=anynans)
        del temp
        th = land_check(th, tdim="doy", anynans=anynans)
        se = land_check(se, tdim="doy", anynans=anynans)
    # assign doy
    ts = add_doy(ts, tdim=tdim, keep_tstep=tstep)

    # Linear interpolation of all consecutive missing blocks
    # of length <= maxPadLength
    # NB by default maxPadLength is None and there is no interpolation
    if maxPadLength:
        ts = ts.interpolate_na(dim=tdim, max_gap=maxPadLength)
    # Flip temp time series if detecting cold spells
    if coldSpells:
        ts = -1.0 * ts

    # Build a pandas series with the positional indexes as values
    # [0,1,2,3,4,5,6,7,8,9,10,..]
    idxarr = pd.Series(data=np.arange(len(ts[tdim])), index=ts[tdim].values)

    # Open list for partial results
    mhwls = []

    # if timeseries is a single point we skipped grid operations
    if point:
        mhwls.append(
            define_events(
                ts,
                th,
                se,
                idxarr,
                minDuration,
                joinGaps,
                maxGap,
                intermediate,
                tdim,
            )
        )
    else:
        # Loop over each cell to detect MHW events, define_events()
        # is delayed, so loop is automatically run in parallel
        for c in ts.cell:
            mhwls.append(
                define_events(
                    ts.sel(cell=c),
                    th.sel(cell=c),
                    se.sel(cell=c),
                    idxarr,
                    minDuration,
                    joinGaps,
                    maxGap,
                    intermediate,
                    tdim,
                )
            )
    results = dask.compute(mhwls)

    # Concatenate results and save as dataset
    # re-assign dimensions previously used to stack arrays
    if point:
        mhw_results = [r[0] for r in results[0]]
        mhw = mhw_results[0]
        if intermediate:
            inter_results = [r[1] for r in results[0]]
            mhw_inter = inter_results[0]
    else:
        dims = list(ts.cell.coords)
        mhw_results = [r[0].assign_coords({d: r[0][d][0].values for d in dims})
                       for r in results[0]]
        mhw = xr.concat(mhw_results, dim='cell')
        mhw = mhw.set_xindex(dims)
        mhw = mhw.unstack(dim='cell')
        if intermediate:
            inter_results = [r[1].assign_coords({d: r[1][d][0].values for d in dims})
                             for r in results[0]]
            mhw_inter = xr.concat(inter_results, dim='cell')
            mhw_inter = mhw_inter.set_xindex(dims)
            mhw_inter = mhw_inter.unstack('cell')
            mhw_inter = mhw_inter.rename({'index': 'time'})
            mhw_inter = mhw_inter.squeeze(drop=True)

    # Flip climatology and intensities in case of cold spell detection
    if coldSpells:
        mhw = flip_cold(mhw)

    # add previously saved attributes to ds
    mhw = annotate_ds(mhw, ds_attrs, "mhw")
    # add all parameters used to global attributes
    params = f"MHW detected using: {minDuration} days of minimum duration"
    if joinGaps:
        params = (
            params
            + f""";
            events separated by {maxGap} or less days were joined"""
        )
    if coldSpells:
        params = (
            params
            + """;
                cold events were detected instead of heat events"""
        )
    if maxPadLength:
        params = (
            params
            + f""";
            where original timeseries had missing values interpolation
            was used to fill them. Gaps > {maxPadLength} days long were
            left as NaNs;"""
        )
    if anynans:
        params = (
            params
            + """;
            any grid point with even only 1 NaN along time
            axis has been removed from calculation"""
        )
    mhw.attrs["xmhw_parameters"] = params
    if intermediate:
        return mhw, mhw_inter
    return mhw

"""
This module contains classes to handle datasets consisting of many files. They
are inspired by the implemented dataset classes in atmlab written by Gerrit
Holl.

Created by John Mrziglod, June 2017
"""

import atexit
from collections import Iterable, OrderedDict
from datetime import datetime, timedelta
import glob
from itertools import tee
import json
from multiprocessing import Pool
import numbers
import os.path
import re
import shutil
import time
import warnings

import numpy as np
import pandas as pd
import typhon.files
import typhon.plots
from typhon.spareice.handlers import CSV, FileInfo, NetCDF4
from typhon.trees import IntervalTree

__all__ = [
    "Dataset",
    "DatasetManager",
    "InhomogeneousFilesError",
    "NoFilesError",
    "NoHandlerError",
    "UnknownPlaceholderError",
    "PlaceholderRegexError",
]


class InhomogeneousFilesError(Exception):
    """Should be raised if the files of a dataset do not have the same internal
    structure but it is required.
    """
    def __init__(self, *args):
        Exception.__init__(self, *args)


class NoFilesError(Exception):
    """Should be raised if no files were found by the :meth:`find_files`
    method.

    """
    def __init__(self, name, start, end, *args):
        message = \
            "Found no files for %s between %s and %s!\nMaybe you "\
            "misspelled the files path? Or maybe there are "\
            "no files for this time period?" % (name, start, end)
        Exception.__init__(self, message, *args)


class NoHandlerError(Exception):
    """Should be raised if no file handler is specified in a dataset object but
    a handler is required.
    """
    def __init__(self, *args):
        Exception.__init__(self, *args)


class UnknownPlaceholderError(Exception):
    """Should be raised if a placeholder was found that was not defined before
    or cannot be filled.
    """
    def __init__(self, name, placeholder_name=None, *args):
        if placeholder_name is None:
            message = \
                "The path of '%s' contains a unknown placeholder!" % (name,)
        else:
            message = \
                "The dataset '%s' does not know the placeholder %s!" % (
                    name, placeholder_name)
        Exception.__init__(self, message, *args)


class PlaceholderRegexError(Exception):
    """Should be raised if the regex of a placeholder is broken.
    """
    def __init__(self, name, placeholder_name=None, ):
        if placeholder_name is None:
            placeholder_name = "one"

        message = \
            "The regex of %s placeholder is broken from the '%s' dataset." % (
                placeholder_name, name)
        Exception.__init__(self, message)


class Dataset:
    """Class which provides methods to handle a set of multiple files
    (dataset).

    """

    # Required temporal placeholders that can be overridden by the user but
    # not deleted:
    _time_placeholder = {
        # "placeholder_name": [regex to find the placeholder]
        "year": "(\d{4})",
        "year2": "(\d{2})",
        "month": "(\d{2})",
        "day": "(\d{2})",
        "doy": "(\d{3})",
        "hour": "(\d{2})",
        "minute": "(\d{2})",
        "second": "(\d{2})",
        "millisecond": "(\d{3})",
        "end_year": "(\d{4})",
        "end_year2": "(\d{2})",
        "end_month": "(\d{2})",
        "end_day": "(\d{2})",
        "end_doy": "(\d{3})",
        "end_hour": "(\d{2})",
        "end_minute": "(\d{2})",
        "end_second": "(\d{2})",
        "end_millisecond": "(\d{3})",
    }

    _temporal_resolution = OrderedDict({
        # time placeholder: [pandas frequency, resolution rank]
        "year": ["1A", 0],
        "month": ["1M", 1],
        "day": ["1D", 2],
        "hour": ["1H", 3],
        "minute": ["1T", 4],
        "second": ["1S", 5],
        "millisecond": ["1L", 6],
    })

    # If one has a year with two-digit representation, all years equal or
    # higher than this threshold are based onto 1900, all years below are based
    # onto 2000.
    year2_threshold = 65

    # Placeholders that can be changed by the user:
    placeholder = {}

    # TODO: Should this be a default filling for the placeholders?
    placeholder_filling = {}

    def __init__(
            self, path, handler=None, name=None, info_via=None,
            time_coverage=None, info_cache=None, exclude=None,
            placeholder=None, max_processes=None,
            compress=True, decompress=True,
    ):
        """Initializes a dataset object.

        Args:
            path: A string with the complete path to the dataset files. The
                string can contain placeholder such as {year}, {month},
                etc. See below for a complete list. The direct use of
                restricted regular expressions is also possible. Please note
                that instead of dots '.' the asterisk '\*' is interpreted as
                wildcard. If no placeholders are given, the path must point to
                a file. This dataset is then seen as a single file dataset.
                You can also define your own placeholders by using the
                parameter *placeholder*.
            name: The name of the dataset.
            handler: An object which can handle the dataset files.
                This dataset class does not care which format its files have
                when this file handler object is given. You can use a file
                handler class from typhon.handlers, use
                :class:`~typhon.spareice.handlers.FileHandler` or write your
                own class. If no file handler is given, an adequate one is
                automatically selected for the most common filename suffixes.
                Please note that if no file handler is specified (and none
                could set automatically), this dataset's functionality is
                restricted.
            info_via: Defines how further information about the file will
                be retrieved (e.g. time coverage). Possible options are
                *filename*, *handler* or *both*. Default is *filename*. That
                means that the placeholders in the file's path will be parsed
                to obtain information. If this is *handler*, the
                :meth:`~typhon.spareice.handlers.FileInfo.get_info` method is
                used. If this is *both*, both options will be executed but the
                information from the file handler overwrites conflicting
                information from the filename.
            info_cache: Retrieving further information (such as time coverage)
                about a file may take a while, especially when *get_info* is
                set to *handler*. Therefore, if the file information is cached,
                multiple calls of :meth:`find_files` (for time periods that
                are close) are significantly faster. Specify a name to a file
                here (which need not exist) if you wish to save the information
                data to a file. When restarting your script, this cache is
                used.
            time_coverage: If this dataset consists of multiple files, this
                parameter is the relative time coverage (i.e. a timedelta, e.g.
                "1 hour") of each file. If the ending time of a file cannot be
                retrieved by its file handler or filename, it is then its
                starting time + *time_coverage*. Can be a timedelta object or
                a string with time information (e.g. "2 seconds"). Otherwise
                the missing ending time of each file will be set to its
                starting time. If this
                dataset consists of a single file, then this is its absolute
                time coverage. Set this to a tuple of timestamps (datetime
                objects or strings). Otherwise the period between year 1 and
                9999 will be used as a default time coverage.
            exclude: A list of time periods (tuples of two timestamps) that
                will be excluded when searching for files of this dataset.
            placeholder: A dictionary with pairs of placeholder name matching
                regular expression. These are user-defined placeholders, the
                standard temporal placeholders do not have to be defined.
            max_processes: Maximal number of parallel processes that will be
                used for :meth:`~typhon.spareice.datasets.Dataset.map` or
                :meth:`~typhon.spareice.datasets.Dataset.map_content` like
                methods per default (default is the number of CPUs).
            compress: If true and the *path* path ends with a compression
                suffix (such as *.zip*, *.gz*, *.b2z*, etc.), newly created
                dataset files will be compressed after writing them to disk.
                Default value is true.
            decompress: If true and the *path* path ends with a compression
                suffix (such as *.zip*, *.gz*, *.b2z*, etc.), dataset files
                will be decompressed before reading them. Default value is
                true.

        Allowed placeholders in the *path* argument are:

        +-------------+------------------------------------------+------------+
        | Placeholder | Description                              | Example    |
        +=============+==========================================+============+
        | year        | Four digits indicating the year.         | 1999       |
        +-------------+------------------------------------------+------------+
        | year2       | Two digits indicating the year. [1]_     | 58 (=2058) |
        +-------------+------------------------------------------+------------+
        | month       | Two digits indicating the month.         | 09         |
        +-------------+------------------------------------------+------------+
        | day         | Two digits indicating the day.           | 08         |
        +-------------+------------------------------------------+------------+
        | doy         | Three digits indicating the day of       | 002        |
        |             | the year.                                |            |
        +-------------+------------------------------------------+------------+
        | hour        | Two digits indicating the hour.          | 22         |
        +-------------+------------------------------------------+------------+
        | minute      | Two digits indicating the minute.        | 58         |
        +-------------+------------------------------------------+------------+
        | second      | Two digits indicating the second.        | 58         |
        +-------------+------------------------------------------+------------+
        | millisecond | Three digits indicating the millisecond. | 999        |
        +-------------+------------------------------------------+------------+
        .. [1] Numbers lower than 65 are interpreted as 20XX while numbers
            equal or greater are interpreted as 19XX (e.g. 65 = 1965,
            99 = 1999)

        All those place holders are also allowed to have the prefix *end*
        (e.g. *end_year*). They represent the end of the time coverage.

        Examples:

        .. code-block:: python

            ### Multi file dataset ###
            # Define a dataset consisting of multiple files:
            dataset = Dataset(
                path="/dir/{year}/{month}/{day}/{hour}{minute}{second}.nc",
                name="TestData",
                # If the time coverage of the data cannot be retrieved from the
                # filename, you should set this to "handler" and giving a file
                # handler to this object:
                info_via="filename"
            )

            # Find some files of the dataset:
            for file, times in dataset.find_files("2017-01-01", "2017-01-02"):
                # Should print some files such as "/dir/2017/01/01/120000.nc":
                print(file)

            ### Single file dataset ###
            # Define a dataset consisting of a single file:
            dataset = Dataset(
                # Simply use the path without placeholders:
                path="/path/to/file.nc",
                name="TestData2",
                # The time coverage of the data cannot be retrieved from the
                # filename (because there are no placeholders). You can use the
                # file handler get_info() method via "content" or you can
                # define the time coverage here directly:
                time_coverage=("2007-01-01 13:00:00", "2007-01-14 13:00:00")
            )

            ### Play with the time_coverage parameter ###
            # Define a dataset with daily files:
            dataset = Dataset("/dir/{year}/{month}/{day}.nc")

            file = dataset.get_info(
                "/dir/2017/11/12.nc"
            )
            print(file)
            # /dir/2017/11/12.nc
            #   Start: 2017-11-12
            #   End: 2017-11-12

            file = dataset.get_info(
                "/dir/2017/11/12.nc"
            )
            print("Start:", file.times[0])
            print("End:", file.times[1])
            # Start: 2017-11-12
            # End: 2017-11-12

        """

        # Initialize member variables:
        self._name = None
        self.name = name

        # Flag wether this is a single file dataset (will be derived in the
        # path setter method automatically):
        self.single_file = None

        # The path parameters (will be set and documented in the path setter
        # method):
        self._path = None
        self._path_placeholders = None
        self._path_temporal_resolution = None
        self._path_start_time_placeholders = None
        self._path_end_time_placeholders = None
        self._path_end_time_overshooting_compensator = None
        self._dir_placeholders = None
        self._dir_temporal_resolution = None
        self.path = path

        if handler is None:
            # Try to derive the file handler from the files extension but
            # before we might remove potential compression suffixes:
            basename, extension = os.path.splitext(self.path)
            if typhon.files.is_compression_format(extension.lstrip(".")):
                _, extension = os.path.splitext(basename)

            if extension == ".nc" or extension == ".h5":
                self.handler = NetCDF4()
            elif extension == ".txt" or extension == ".asc" \
                    or extension == ".csv":
                self.handler = CSV()
            else:
                self.handler = None
        else:
            self.handler = handler

        # Defines which method will be used by .get_info():
        if info_via is None:
            self.info_via = "filename"
        else:
            if self.handler is None:
                raise NoHandlerError(
                    "Cannot set 'info_via' to '%s'! No file handler is "
                    "specified!".format(info_via))
            else:
                self.info_via = info_via

        # A list of time periods that will be excluded when searching files:
        self._exclude = None
        self.exclude = exclude

        if placeholder is not None:
            self.placeholder = placeholder

        self.max_processes = max_processes
        self.compress = compress
        self.decompress = decompress

        self._time_coverage = None
        self.time_coverage = time_coverage

        # Multiple calls of .find_files() can be very slow when using a time
        # coverage retrieving method "content". Hence, we use a cache to
        # store the names and time coverages of already touched files in this
        # dictionary.
        self.info_cache_filename = info_cache
        self.info_cache = {}
        if self.info_cache_filename is not None:
            try:
                # Load the time coverages from a file:
                self.load_info_cache(self.info_cache_filename)
            except Exception as e:
                raise e
            else:
                # Save the time coverages cache into a file before exiting.
                # This will be executed as well when the python code is
                # aborted due to an exception. This is normally okay, but what
                # happens if the error occurs during the loading of the time
                # coverages? We would overwrite the cache with nonsense.
                # Therefore, we need this code in this else block.
                atexit.register(
                    Dataset.save_info_cache,
                    self, self.info_cache_filename)

    """def __iter__(self):
        return self

    def __next__(self):
        # We split the path of the input files after the first appearance of 
        # {day} or {doy}.
        path_parts = re.split(r'({\w+})', self.path)

        for dir in self._find_subdirs(path_parts[0])
            print(path_parts)

            yield file
    """
    def __contains__(self, item):
        """Checks whether a timestamp is covered by this dataset.

        Notes:
            This only gives proper results if the dataset consists of
            continuous data (files that covers a time span instead of only one
            timestamp).

        Args:
            item: Either a string with time information or datetime object.
                Can be also a tuple or list of strings / datetime objects that
                will be checked.

        Returns:
            True if timestamp is covered.
        """
        if isinstance(item, (tuple, list)):
            for elem in item:
                if elem not in self:
                    return False

            return True
        else:
            start = self._to_datetime(item)
            end = start + timedelta(microseconds=1)
            try:
                next(self.find_files(start, end,
                                     no_files_error=False, sort=False,))
                return True
            except StopIteration:
                return False

    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(self.read_period(item.start, item.stop))
        elif isinstance(item, (datetime, str)):
            filename = self.find_file(item)
            if filename is not None:
                return self.read(filename)
            return None

    def __setitem__(self, key, value):
        if isinstance(key, slice):
            start = key.start
            end = key.stop
        else:
            start = end = key

        filename = self.generate_filename((start, end))
        self.write(filename, value)

    def __repr__(self):
        return str(self)

    def __str__(self):
        dtype = "Single-File" if self.single_file else "Multi-File"

        info = "Name:\t" + self.name
        info += "\nType:\t" + dtype
        info += "\nFiles path:\t" + self.path
        return info

    @staticmethod
    def _call_function_with_file_info(args):
        """ This is a small wrapper function to call the function that is
        called on dataset files via .map().

        Args:
            args: A tuple containing following elements:
                (Dataset object, file_info, function,
                function_arguments)

        Returns:
            The return value of *function* called with the arguments *args*.
        """
        dataset, file_info, func, function_arguments, output, \
            return_file_info, verbose = args

        if verbose:
            print("Process %s ()" % file_info)

        if function_arguments is None:
            return_value = func(dataset, file_info)
        else:
            return_value = func(
                dataset, file_info, **function_arguments)

        if output is None:
            if return_file_info:
                return file_info, return_value
            else:
                return return_value
        else:
            # file_info could be a bundle of files
            if isinstance(file_info, list):
                start_times, end_times = zip(
                    *(
                        file.times
                        for file in file_info
                    )
                )
                new_filename = output.generate_filename(
                    (min(start_times), max(end_times)),
                )
            else:
                new_filename = output.generate_filename(
                    file_info.times, fill=file_info.attr
                )

            output.write(new_filename, return_value)
            return file_info

    @staticmethod
    def _call_function_with_file_content(args):
        """ This is a small wrapper function to call a function on an object
        returned by reading a dataset file via Dataset.read().

        Args:
            args: A tuple containing following elements:
                (Dataset object, file_info, func,
                 function_arguments, read_arguments, output)

        Returns:
            The return value of *method* called with the arguments
            *method_arguments*.
        """
        dataset, file_info, func, function_arguments, output, \
            reading_arguments, return_file_info, verbose = args

        # file_info could be a bundle of files
        if isinstance(file_info, FileInfo):
            times = file_info.times
        else:
            start_times, end_times = zip(
                *(file.times for file in file_info)
            )
            times = min(start_times), max(end_times)

        if verbose:
            print("Process content from {} to {} ({} files)".format(
                *times,
                len(file_info) if isinstance(file_info, Iterable) else 1
            ))

        if reading_arguments is None:
            reading_arguments = {}

        if isinstance(file_info, FileInfo):
            data = dataset.read(file_info, **reading_arguments)
        else:
            data = [
                dataset.read(file, **reading_arguments)
                for file in file_info
            ]

        if function_arguments is None:
            function_arguments = {}

        return_value = func(data, **function_arguments)

        if output is None:
            if return_file_info:
                return file_info, return_value
            else:
                return return_value
        elif return_value is not None:
            if isinstance(file_info, FileInfo):
                placeholder_filling = file_info.attr
            else:
                placeholder_filling = file_info[0].attr

            new_filename = output.generate_filename(
                times, fill=placeholder_filling
            )
            output.write(new_filename, return_value)

        return file_info

    @staticmethod
    def _copy_file(
            dataset, filename, time_coverage,
            path, converter, delete_originals):
        """This is a small wrapper function for copying files. Do not use it
        directly but :meth:`Dataset.copy` instead.

        Args:
            dataset:
            filename:
            time_coverage:
            path:
            converter:
            delete_originals:

        Returns:
            None
        """
        # Generate the new file name
        new_filename = dataset.generate_filename(
            path, *time_coverage)

        # Create the new directory if necessary.
        os.makedirs(os.path.dirname(new_filename), exist_ok=True)

        # Shall we simply copy or even convert the files?
        if converter is None:
            if delete_originals:
                print("\tDelete:", filename)
                shutil.move(filename, new_filename)
            else:
                shutil.copy(filename, new_filename)
        else:
            # Read the file with the current file handler
            data = dataset.read(filename)

            # Store the data of the file with the new file handler
            converter.write(new_filename, data)

            if delete_originals:
                print("\tDelete:", filename)
                os.remove(filename)

    def copy(
            self, start, end, destination,
            converter=None, delete_originals=False, verbose=False,
            new_name=None
    ):
        """ Copies all files from this dataset between two dates to another
        location.

        When passing a file handler via the argument converter, it also
        converts all matched files to a new format defined by the passed
        file handler.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            destination: The new path of the files. Must contain place holders
                (such as {year}, {month}, etc.).
            converter: If you want to convert the files during copying to a
                different format, you can pass a file handler object with
                writing-to-file support here.
            delete_originals: If true, then all copied original files will be
                deleted. Be careful, this cannot get undone!
            verbose: If true, it prints debug messages during copying.
            new_name: The name of the new dataset. If it is not given,
                the new name is the the old name followed by "_copy".

        Returns:
            New Dataset object with the new files.

        Examples:

        .. code-block:: python

            # Copy all the files between the 15th and 23rd September 2016:
            date1 = datetime(2017, 9, 15)
            date2 = datetime(2017, 9, 23)
            old_dataset = Dataset(
                "old/path/{year}/{month}/{day}/{hour}{minute}{second}.jpg",
                handler=FileHandlerJPG()
            )
            new_dataset = old_dataset.copy(
                date1, date2,
                "new/path/{year}/{month}/{day}/{hour}{minute}{second}.jpg",
            )

        .. code-block:: python

            # When you want to convert the files during copying:
            old_dataset = Dataset(
                "old/path/{year}/{month}/{day}/{hour}{minute}{second}.jpg",
                handler=FileHandlerJPG()
            )
            # Note that this only works if the converter file handler
            # (FileHandlerPNG in this example) supports
            # writing to a file.
            new_dataset = old_dataset.copy(
                date1, date2,
                "new/path/{year}/{month}/{day}/{hour}{minute}{second}.png",
                converter=FileHandlerPNG(),
            )
        """

        # If the new path contains place holders, fill them for each file.
        # Otherwise it is a path which does not describe each file
        # individually. So far, we cannot handle this.
        # TODO: Adjust this solution for single file datasets.
        # TODO: Is it helpful for the performance to use multiple processes
        # TODO: here?
        if "{" in destination:
            # Copy the files
            self.map(
                start, end, Dataset._copy_file,
                {
                     "path" : destination,
                     "converter" : converter,
                     "delete_originals" : delete_originals
                },
                verbose=verbose
            )
        else:
            if self.single_file:
                # TODO: Copy single file
                raise NotImplementedError("Copying single files is not yet "
                                          "implemented!")
            else:
                raise ValueError(
                    "The new_path argument must describe each file "
                    "individually by using place holders!")

        # Copy this dataset object but change the path.
        new_dataset = Dataset(
            destination,
            new_name if new_name is not None else self.name + "_copy",
            self.handler, self.time_coverage
        )

        if converter is not None:
            # The files are in a different format now. Hence, we need the new
            # file handler:
            new_dataset.handler = converter

        return new_dataset

    @property
    def exclude(self):
        """Gets or sets time periods that will be excluded when searching for
        files.

        Returns:
            A IntervalTree object.
        """
        return self._exclude

    @exclude.setter
    def exclude(self, value):
        if value is None:
            self._exclude = None
        else:
            if isinstance(value, np.ndarray):
                self._exclude = IntervalTree(value)
            else:
                self._exclude = IntervalTree(np.array(value))

    def find_file(self, timestamp, fill=None):
        """Finds either the file that covers a timestamp or is the closest to
        it.

        This method ignores the value of *Dataset.exclude*.

        Args:
            timestamp: date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            fill: A dictionary with fillings for user-defined placeholder.

        Returns:
            The FileInfo object of the found file. If no file was found, a
            NoFilesError is raised.
        """

        # Special case: the whole dataset consists of one file only.
        if self.single_file:
            if os.path.isfile(self.path):
                # We do not have to check the time coverage since there this is
                # automatically the closest file to the timestamp.
                return self.path
            else:
                raise ValueError(
                    "The path parameter of '%s' does not contain placeholders"
                    " and is not a path to an existing file!" % self.name)

        timestamp = self._to_datetime(timestamp)

        if fill is None:
            fill = {}

        # We might need some more fillings than given by the user therefore
        # we need the error catching:
        try:
            # Maybe there is a file with exact this timestamp?
            path = self.generate_filename(timestamp, fill=fill)

            if os.path.isfile(path):
                return self.get_info(path)
        except UnknownPlaceholderError:
            pass

        # We need all possible files that are close to the timestamp hence we
        # need the search dir for those files:
        search_dir = self.generate_filename(
            timestamp, template=os.path.dirname(self.path), fill=fill
        )

        regex = self._prepare_regex()

        files = list(self._get_files(
            search_dir, regex, datetime.min, datetime.max, False
        ))

        if not files:
            return None

        times = [file.times for file in files]

        # Either we find a file that covers the certain timestamp:
        for index, time_coverage in enumerate(times):
            if IntervalTree.interval_contains(time_coverage, timestamp):
                return files[index]

        # Or we find the closest file.
        intervals = np.min(np.abs(np.asarray(times) - timestamp), axis=1)
        return files[np.argmin(intervals)]

    def find_files(
            self, start, end, sort=True, bundle=None, fill=None,
            no_files_error=True, verbose=False,
    ):
        """ Find all files of this dataset in a given time period.

        The *start* and *end* parameters build a semi-open interval: only the
        files that are equal or newer than *start* and older than *end* are
        going to be found.

        While searching this method checks whether the file lies in the time
        periods given by *Dataset.exclude*.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            sort: If true, all files will be yielded
                sorted by their starting time. Default is true.
            bundle: Instead of only yielding one file at a time, you can get a
                bundle of files. There are two possibilities: by setting this
                to an integer, you can define the size of the bundle directly
                or by setting this to a string (e.g. *1H*),
                you can define the time period of one bundle. See
                http://pandas.pydata.org/pandas-docs/stable/timeseries.html#offset-aliases
                for allowed time specifications. Default value is 1. This
                argument will be ignored when having a single-file dataset.
                When using *bundle*, the returned files will always be sorted
                ignoring the state of the *sort* argument.
            no_files_error: If true, raises an NoFilesError when no
                files are found.
            verbose: If true, debug messages will be printed.

        Yields:
            Either a :class:`~typhon.spareice.handlers.FileInfo` object for
            each found file or - if *bundle_size* is not None - a list of
            :class:`~typhon.spareice.handlers.FileInfo` objects.

        Examples:

        .. code-block:: python

            # Define a dataset consisting of multiple files:
            dataset = Dataset(
                "/dir/{year}/{month}/{day}/{hour}{minute}{second}.nc"
            )

            # Find some files of the dataset:
            for file in dataset.find_files("2017-01-01", "2017-01-02"):
                # file is a FileInfo object that has the attribute path
                # and times.
                print(file.path)  # e.g. "/dir/2017/01/01/120000.nc"
                print(file.times)  # list of two datetime objects
        """

        # The user can give strings instead of datetime objects:
        start = self._to_datetime(start)
        end = self._to_datetime(end)

        if verbose:
            print("Find files between %s and %s!" % (start, end))

        # We want to have a semi-open interval as explained in the doc string.
        end -= timedelta(microseconds=1)

        # Special case: the whole dataset consists of one file only.
        if self.single_file:
            if os.path.isfile(self.path):
                file_info = self.get_info(self.path)
                if IntervalTree.interval_overlaps(
                        file_info.times, (start, end)):
                    yield file_info
                elif no_files_error:
                    raise NoFilesError(self.name, start, end)
                return
            else:
                raise ValueError(
                    "The path of '%s' neither contains placeholders"
                    " nor is a path to an existing file!" % self.name)

        regex = self._prepare_regex()

        # Find all files by iterating over all searching paths and check
        # whether they match the path regex and the time period.
        file_finder = (
            file_info
            for path in self._get_search_dirs(start, end, verbose)
            for file_info in self._get_files(path, regex, start, end, verbose)
        )

        # Even if no files were found, the user does not want to know.
        if not no_files_error:
            yield from self._prepare_find_files_return(
                file_finder, sort, bundle)
            return

        # The users wants an error to be raised if no files were found. Since
        # the file_finder is an iterator, we have to check whether it is empty.
        # I do not know whether there is a more pythonic way but Matthew
        # Flaschen shows how to do it with itertools.tee:
        # https://stackoverflow.com/a/3114423
        return_files, check_files = tee(file_finder)
        try:
            next(check_files)

            # We have found some files and can return them
            yield from self._prepare_find_files_return(
                return_files, sort, bundle)
        except StopIteration:
            raise NoFilesError(self.name, start, end)

    def find_overlapping_files(
            self, start, end, other_dataset, max_interval=None, verbose=False):
        """Find files between two datasets that overlap in time.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            other_dataset: A Dataset object which holds the other files.
            max_interval: Maximal time interval in seconds between
                two overlapping files. Must be an integer or float.
            verbose: If true, debugging messages will be printed.

        Yields:
            A tuple with the names of two files which correspond to each other.
        """
        if max_interval is not None:
            max_interval = self._to_timedelta(max_interval)
            start = self._to_datetime(start) - max_interval
            end = self._to_datetime(end) + max_interval

        files1 = list(
            self.find_files(start, end, verbose=verbose)
        )
        files2 = list(
            other_dataset.find_files(start, end, verbose=verbose)
        )

        # Convert the times (datetime objects) to seconds (integer)
        times1 = [
            [int(file.times[0].timestamp()), int(file.times[1].timestamp())]
            for file in files1
        ]
        times2 = np.asarray([
            [file.times[0].timestamp(), file.times[1].timestamp()]
            for file in files2
        ]).astype('int')

        if max_interval is not None:
            # Expand the intervals of the secondary dataset to close-in-time
            # intervals.
            times2[:, 0] -= int(max_interval.total_seconds())
            times2[:, 1] += int(max_interval.total_seconds())

        tree = IntervalTree(times2)

        # Search for all overlapping intervals:
        results = tree.query(times1)

        yield from (
            (files1[i],
             [files2[oi] for oi in sorted(overlapping_files)])
            for i, overlapping_files in enumerate(results)
        )

    def generate_filename(
            self, time_period, template=None, fill=None):
        """ Generate the full path and name of a file for a time period.

        Use :meth:`parse_filename` if you want retrieve information from the
        filename.

        Args:
            time_period: Either a tuple of two datetime objects representing
                start and end time or simply one datetime object (for timestamp
                 files).
            template: A string with format placeholders such as {year} or
                {day}. If not given, the template in *Dataset.files* is used.
            fill: A dictionary with fillings for user-defined placeholder.

        Returns:
            A string containing the full path and name of the file.

        Example:

        .. code-block:: python

            Dataset.generate_filename(
                datetime(2016, 1, 1),
                "{year2}/{month}/{day}.dat",
            )
            # Returns "16/01/01.dat"

            Dataset.generate_filename(
                ("2016-01-01", "2016-12-31"),
                "{year}{month}{day}-{end_year}{end_month}{end_day}.dat",
            )
            # Returns "20160101-20161231.dat"

        """

        if isinstance(time_period, (tuple, list)):
            start_time = Dataset._to_datetime(time_period[0])
            end_time = Dataset._to_datetime(time_period[1])
        else:
            start_time = Dataset._to_datetime(time_period)
            end_time = start_time

        if template is None:
            template = self.path

        if fill is None:
            fill = {}

        try:
            # Fill all placeholders variables with values
            return template.format(
                year=start_time.year, year2=str(start_time.year)[-2:],
                month="{:02d}".format(start_time.month),
                day="{:02d}".format(start_time.day),
                doy="{:03d}".format(
                    (start_time - datetime(start_time.year, 1, 1)).days
                    + 1),
                hour="{:02d}".format(start_time.hour),
                minute="{:02d}".format(start_time.minute),
                second="{:02d}".format(start_time.second),
                millisecond="{:03d}".format(
                    int(start_time.microsecond / 1000)),
                end_year=end_time.year, end_year2=str(end_time.year)[-2:],
                end_month="{:02d}".format(end_time.month),
                end_day="{:02d}".format(end_time.day),
                end_doy="{:03d}".format(
                    (end_time - datetime(end_time.year, 1, 1)).days
                    + 1),
                end_hour="{:02d}".format(end_time.hour),
                end_minute="{:02d}".format(end_time.minute),
                end_second="{:02d}".format(end_time.second),
                end_millisecond="{:03d}".format(
                    int(end_time.microsecond/1000)),
                **fill,
            )
        except KeyError:
            raise UnknownPlaceholderError(self.name)

    def _get_files(self, path, regex, start, end, verbose):
        """Yield files that matches the search conditions.

        Args:
            path: Path to the directory that contains the files that should be
                checked.
            regex: A regular expression that should match the filename.
            start: Datetime that defines the start of a time interval.
            end: Datetime that defines the end of a time interval. The time
                coverage of the file should overlap with this interval.
            verbose: If True, it prints debug messages.

        Yields:
            A FileInfo object with the file path and time coverage
        """
        if verbose:
            print("Check all files in %s" % os.path.join(path, "*"))

        for filename in glob.iglob(os.path.join(path, "*")):
            if regex.match(filename):
                file_info = self.get_info(filename)

                # Test whether the file is overlapping the interval between
                # start and end date.
                if IntervalTree.interval_overlaps(
                        file_info.times, (start, end))\
                        and not self.is_excluded(file_info.times):
                    yield file_info

    def get_info(self, filename, retrieve_via=None):
        """Get information about a file.

        How the information will be retrieved is defined by

        Args:
            filename: Path and name of the file.
            retrieve_via: Defines how further information about the file will
                be retrieved (e.g. time coverage). Possible options are
                *filename*, *handler* or *both*. Default is the value of the
                *info_via* parameter during initialization of this Dataset
                 object. If this is *filename*, the placeholders in the file's
                path will be parsed to obtain information. If this is
                *handler*, the
                :meth:`~typhon.spareice.handlers.FileInfo.get_info` method is
                used. If this is *both*, both options will be executed but the
                information from the file handler overwrites conflicting
                information from the filename.

        Returns:
            A :meth`~typhon.spareice.handler.FileInfo` object.
        """
        if filename in self.info_cache:
            return self.info_cache[filename]

        info = FileInfo(filename)
        if self.single_file:
            info.times = self.time_coverage

        if retrieve_via is None:
            retrieve_via = self.info_via

        if retrieve_via in ("filename", "both"):
            info.update(self.parse_filename(filename))

        if retrieve_via in ("handler", "both"):
            with typhon.files.decompress(filename) as uncompressed_file:
                info.update(self.handler.get_info(uncompressed_file))

        if info.times[0] is None:
            raise ValueError(
                "Could not retrieve the starting time information from "
                "the file '%s' from the %s dataset!"
                % (filename, self.name)
            )

        # Sometimes the files have only a starting time. But if the user has
        # defined a timedelta for the coverage, the ending time can be
        # calculated from them.
        if info.times[1] is None:
            if isinstance(self.time_coverage, timedelta):
                info.times[1] = info.times[0] + self.time_coverage
            else:
                info.times[1] = info.times[0]

        self.info_cache[filename] = info
        return info

    def _get_search_dirs(self, start, end, verbose):
        """Yields all searching directories for a time period.

        Args:
            start: Datetime that defines the start of a time interval.
            end: Datetime that defines the end of a time interval. The time
                coverage of the files should overlap with this interval.
            verbose: If true, it prints debug messages.

        Yields:
            A path as a string.
        """

        dir_template = os.path.dirname(self.path)

        # If the directory does not contain temporal placeholders, we simply
        # return the original directory
        if self._dir_temporal_resolution is None:
            if verbose:
                print("Directory has no temporal placeholders")
            yield dir_template
            return

        # Start one day before the starting date because we may have files
        # overlapping one day.
        times = pd.date_range(
            start.date() - timedelta(days=1), end,
            freq=self._dir_temporal_resolution,
        )

        if verbose:
            print("Searching in approximately %d dirs" % len(times))

        for dir_time in times:
            search_dir = self.generate_filename(dir_time, dir_template)

            if not os.path.isdir(search_dir):
                if verbose:
                    print("\tSkipped: %s" % search_dir)
                continue

            yield search_dir

        return

    @staticmethod
    def _get_superior_time_resolution(placeholders,):
        """Get the superior time resolution of all placeholders.

        Examples:
            The superior time resolution of seconds are minutes, of hours are
            days, etc.

        Args:
            placeholders: A list or dictionary with placeholders.

        Returns:
            A pandas compatible frequency stringo or None if the superior time
            resolution is higher than a year.
        """
        # All placeholders from which we know the resolution:
        placeholders = set(placeholders).intersection(
            Dataset._temporal_resolution
        )

        if not placeholders:
            return None

        # From all temporal placeholders, we want to find the one with the
        # lowest resolution (month > day > hour, etc.).
        # Note: The higher the resolution of the placeholder is the lower its
        # sorting rank is.
        lowest_resolution_index = min(
            (Dataset._temporal_resolution[tp] for tp in placeholders),
            key=lambda x: x[1],
        )[1]

        if lowest_resolution_index == 0:
            return None

        resolutions = list(Dataset._temporal_resolution.values())
        superior_resolution = resolutions[lowest_resolution_index-1][0]

        return pd.Timedelta(superior_resolution).to_pytimedelta()

    @staticmethod
    def _get_time_resolution(placeholders, ):
        """Get the lowest time resolution of all placeholders

        Args:
            placeholders: A list or dictionary with placeholders.

        Returns:
            A pandas compatible frequency string.
        """
        placeholders = set(placeholders)
        if "doy" in placeholders:
            placeholders.remove("doy")
            placeholders.add("day")
        if "year2" in placeholders:
            placeholders.remove("year2")
            placeholders.add("year")

        # All placeholders from which we know the resolution:
        placeholders = set(placeholders).intersection(
            Dataset._temporal_resolution
        )

        if not placeholders:
            return None

        return max(
            (Dataset._temporal_resolution[tp] for tp in placeholders),
            key=lambda x: x[1],
        )[0]

    def is_excluded(self, period):
        """Checks whether a time interval is excluded from this Dataset.

        Args:
            period: A tuple of two datetime objects.

        Returns:
            True or False
        """
        if self.exclude is None:
            return False

        return period in self.exclude

    def load_info_cache(self, filename):
        """ Loads the information cache from a file.

        Returns:
            None
        """
        if filename is not None and os.path.exists(filename):
            print("Load file information of {} dataset from {}.".format(
                self.name, filename))

            try:
                with open(filename) as file:
                    json_info_cache = json.load(file)
                    # Create FileInfo objects from json dictionaries:
                    info_cache = {
                        json_dict["path"]: FileInfo.from_json_dict(json_dict)
                        for json_dict in json_info_cache
                    }
                    self.info_cache.update(info_cache)
            except Exception as e:
                warnings.warn(
                    "Could not load the file information from cache file "
                    "'%s':\n%s." % (filename, e)
                )

    def map(
        self, start, end,
        func, func_arguments=None, output=None, max_processes=None,
        bundle=None, return_file_info=False, process_initializer=None,
        process_initargs=None, verbose=False,
    ):
        """Applies a function on all files of this dataset between two dates.

        This method can use multiple processes to boost the procedure
        significantly. Depending on which system you work, you should try
        different numbers for *max_processes*.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            func: A reference to a function. The function should accept
                at least three arguments: the dataset object, the filename and
                the time coverage of the file (tuple of two datetime objects).
            func_arguments: Additional keyword arguments for the function.
            output: Set this to a Dataset object and the return value of
                *func* will be copied there. In that case
                *include_file_info* will be ignored.
            max_processes: Max. number of parallel processes to use. When
                lacking performance, you should change this number.
            bundle: Instead of only mapping a function onto one file at a time,
                you can map it onto a bundle of files. Look at the
                documentation of the *bundle* argument in
                :meth:`~typhon.spareice.Dataset.find_files` for more details.
            return_file_info: Since the order of the returning results is
                arbitrary, you can include the name of the processed file
                and its time coverage in the results.
            process_initializer: Must be a reference to a function that is
                called once when starting a new process. Can be used to preload
                variables into one process workspace. See also
                https://docs.python.org/3.1/library/multiprocessing.html#module-multiprocessing.pool
                for more information.
            process_initargs: A tuple with arguments for *process_initializer*.
            verbose: If this is true, debug information will be printed.

        Returns:
            A list with one item for each processed file. The order is
            arbitrary. If *return_file_info* is true, the item is a tuple
            of a FileInfo object and the return value of the applied function.
            If *return_file_info* is false, the item is simply the return
            value of the applied function.

        Examples:

        """

        if verbose:
            print("Process all files from %s to %s.\nThis may take a while..."
                  % (start, end))

        # Measure the time for profiling.
        start_time = time.time()

        if max_processes is None:
            max_processes = self.max_processes

        # Create a pool of processes and process all the files with them.
        pool = Pool(
            max_processes, initializer=process_initializer,
            initargs=process_initargs,
        )

        args = (
            (self, x, func, func_arguments, output, return_file_info, verbose)
            for x in self.find_files(start, end, sort=False, bundle=bundle, )
        )

        results = pool.map(
            Dataset._call_function_with_file_info,
            args, #chunksize=10,
        )

        if verbose:
                print("It took %.2f seconds using %d parallel processes to "
                      "process %d files." % (
                        time.time() - start_time, max_processes, len(results)))

        return results

    def map_content(
            self, start, end,
            func, func_arguments=None, output=None, reading_arguments=None,
            max_processes=None, bundle=None, return_file_info=False,
            process_initializer=None, process_initargs=None,
            verbose=False):
        """Applies a method on the content of each file of this dataset between
        two dates.

        This method is similar to Dataset.map() but each file will be read
        before the given function will be applied.

        This method can use multiple processes to boost the procedure
        significantly. Depending on which system you work, you should try
        different numbers for *max_processes*.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            func: A reference to a function. The function should expect as
                first argument the content object which is returned by the file
                handler's *read* method.
            func_arguments: Additional keyword arguments for the function.
            reading_arguments: Additional keyword arguments that will be passed
                to the reading function (see Dataset.read() for more
                information).
            output: Set this to a Dataset object and the return value of
                *func* will be copied there. In that case
                *return_file_info* will be ignored.
            max_processes: Max. number of parallel processes to use. When
                lacking performance, you should change this number.
            return_file_info: Since the order of the returning results is
                arbitrary, you can include the name of the processed file
                and its time coverage in the results.
            process_initializer: Must be a reference to a function that is
                called once when starting a new process. Can be used to preload
                variables into one process workspace. See also
                https://docs.python.org/3.1/library/multiprocessing.html#module-multiprocessing.pool
                for more information.
            process_initargs: A tuple with arguments for *process_initializer*.
            bundle: Instead of only mapping a function onto one file at a time,
                you can map it onto a bundle of files. Look at the
                documentation of the *bundle* argument in
                :meth:`~typhon.spareice.Dataset.find_files` for more details.
            verbose: If  true, debug information will be printed.

        Returns:
            A list with one item for each processed file. The order is
            arbitrary.
            If *output* is set to a Dataset object, only a list with all
                processed files is returned.
            If *return_file_info* is true, the item is a tuple of a FileInfo
                object and the return value of the applied function.
            If *return_file_info* is false, it is simply the return value
                of the applied function.

        Examples:

        """

        if verbose:
            print("Process all files from %s to %s.\nThis may take a while..."
                  % (start, end))

            # Measure the time for profiling.
            start_time = time.time()

        if max_processes is None:
            max_processes = self.max_processes

        # Create a pool of processes and process all the files with them.
        pool = Pool(
            max_processes, initializer=process_initializer,
            initargs=process_initargs,
        )

        # Prepare argument list for mapping function
        args = (
            (self, x, func, func_arguments, output, reading_arguments,
             return_file_info, verbose)
            for x in self.find_files(start, end, sort=False, bundle=bundle, )
        )

        results = pool.map(
            Dataset._call_function_with_file_content,
            args, #chunksize=10,
        )

        if verbose:
            print("It took %.2f seconds using %d parallel processes to "
                  "process %d files." % (
                    time.time() - start_time, max_processes, len(results)))

        return results

    @property
    def name(self):
        """Gets or sets the dataset's name.

        Returns:
            A string with the dataset's name.
        """
        return self._name

    @name.setter
    def name(self, value):
        if value is None:
            value = str(id(self))

        self._name = value

    def parse_filename(self, filename, no_time=False):
        """Parse the filename with temporal and additional regular expressions.

        This method uses the standard temporal placeholders which might be
        overwritten by the user-defined placeholders in *Dataset.placeholder*.

        Args:
            filename: Path and name of the file.
            no_time: If true, only the user-defined placeholders will be
                parsed.

        Returns:
            A FileInfo object with the attributes *times* (containing the time
            coverage of the file) and the attribute *attr* which is a
            dictionary of pairs of placeholder and its parsed value.
        """

        regex = self._prepare_regex()
        try:
            values = regex.findall(filename)
            values = values[0]
        except IndexError:
            raise ValueError(
                "Could not parse the filename; it does not match the given "
                "template from the parameter 'files'.")

        try:
            # The temporal placeholder must be converted to integers:
            filled_placeholder = {
                placeholder: int(values[index])
                if placeholder in self._time_placeholder else values[index]
                for index, placeholder in enumerate(self._path_placeholders)
            }
        except IndexError:
            raise PlaceholderRegexError(self.name, None)

        # Retrieve only the time coverage if the user wants us to do it:
        if no_time:
            times = None
        else:
            # Filter out all non temporal placeholders
            times = self._retrieve_time_coverage(
                filled_placeholder
            )

        return FileInfo(
            filename, times,
            # Filter out all placeholder that are not coming from the user
            {k: v for k, v in filled_placeholder.items()
             if k in self.placeholder}
        )

    @property
    def path(self):
        """Gets or sets the path to the dataset's files.

        Returns:
            A string with the path (can contain placeholders or wildcards.)
        """
        if os.path.isabs(self._path):
            return self._path
        else:
            return os.path.join(os.getcwd(), self._path)

    @path.setter
    def path(self, value):
        if value is None:
            raise ValueError("The path parameter cannot be None!")

        self._path = value

        # Get the placeholders from directory (the path excluding the filename)
        self._dir_placeholders = re.findall(
            "\{(\w+)\}", os.path.dirname(self.path))
        self._dir_temporal_resolution = \
            self._get_time_resolution(self._dir_placeholders, )

        # TODO: Currently, we cannot work with files which directory name
        # TODO: contain regular expressions
        if set(self._dir_placeholders).intersection(self.placeholder):
            raise ValueError("Currently, user-defined placeholders in the "
                             "directory name are not supported!")

        # Retrieve the used placeholder names from the path and directory:
        self._path_placeholders = re.findall("\{(\w+)\}", self.path)

        # Get all temporal placeholders from the path (for starting and ending
        # time):
        self._path_start_time_placeholders = {
            p for p in self._path_placeholders
            if not p.startswith("end") and p in self._time_placeholder
        }
        self._path_end_time_placeholders = {
            p.lstrip("end_") for p in self._path_placeholders
            if p.startswith("end") and p in self._time_placeholder
        }

        # If the end time retrieved from the path is younger than the start
        # time, the end time will be incremented by this value:
        self._path_end_time_overshooting_compensator = \
            self._get_superior_time_resolution(
                self._path_end_time_placeholders)


        # Flag whether this is a single file dataset or not:
        no_temporal_placeholders = \
            not set(self._time_placeholder).intersection(
                self._path_placeholders)
        self.single_file = no_temporal_placeholders and "*" not in self.path

        if self.single_file and self._path_placeholders:
            raise ValueError(
                "Placeholders in the files path are not allowed for "
                "single file datasets!")

    @staticmethod
    def _prepare_find_files_return(file_iterator, sort, bundle_size):
        """Prepares the return value of the find_files method.

        Args:
            file_iterator: Generator function that yields the found files.
            sort: If true, all found files will be sorted according to their
                starting times.
            bundle_size: See the documentation of the *bundle* argument in
                :meth`find_files` method.

        Yields:
            Either one FileInfo object or - if bundle_size is set - a list of
            FileInfo objects.
        """
        # We want to have sorted files if we want to bundle them.
        if sort or isinstance(bundle_size, int):
            file_iterator = sorted(file_iterator, key=lambda x: x.times[0])

        if bundle_size is None:
            yield from file_iterator
            return

        # The argument bundle was defined. Either it sets the bundle size
        # directly via a number or indirectly by setting time periods.
        if isinstance(bundle_size, int):
            files = list(file_iterator)

            yield from (
                files[i:i + bundle_size]
                for i in range(0, len(files), bundle_size)
            )
        elif isinstance(bundle_size, str):
            files = list(file_iterator)

            # We want to split the files into hourly (or daily, etc.) bundles.
            # pandas provides a practical grouping function.
            time_series = pd.Series(
                files,
                [file.times[0] for file in files]
            )
            yield from (
                bundle[1].values.tolist()
                for bundle in time_series.groupby(
                    pd.Grouper(freq=bundle_size))
                if bundle[1].any()
            )
        else:
            raise ValueError(
                "The parameter bundle must be a integer or string!")

    def _prepare_regex(self):
        placeholder = self._time_placeholder.copy()
        placeholder.update(self.placeholder)

        path = self.path

        # Mask all dots and convert the asterisk to a regular expression:
        path = path.replace(".", "\.")
        path = path.replace("*", ".*?")

        try:
            # Prepare the regex for the file path
            regex = path.format(**placeholder)
        except KeyError as err:
            raise UnknownPlaceholderError(self.name, str(err))

        return re.compile(regex)

    def read(self, filename, **reading_arguments):
        """Opens and reads a file.

        Notes:
            You need to specify a file handler for this dataset before you
            can use this method.

        Args:
            filename: A string, path-alike object or an iterable of
            **reading_arguments: Additional key word arguments for the
                *read* method of the used file handler class.

        Returns:
            The content of the read file.
        """
        if self.handler is None:
            raise NoHandlerError(
                "Could not get read the file '{}'! No file handler is "
                "specified!".format(filename))

        if isinstance(filename, FileInfo):
            filename = filename.path

        if self.decompress:
            with typhon.files.decompress(filename) as file:
                return self.handler.read(file, **reading_arguments)
        else:
            return self.handler.read(filename, **reading_arguments)

    def read_period(self, start, end, sort=True, **reading_arguments):
        """Reads all files between two dates and returns their content sorted
        by their starting time.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            sort: Sort the files by their starting time.
            **reading_arguments: Additional key word arguments for the
                *read* method of the used file handler class.

        Yields:
            The content of the read file.
        """
        for file in self.find_files(start, end, sort=sort):
            data = self.read(file, **reading_arguments)
            if data is not None:
                yield data

    def _retrieve_time_coverage(self, filled_placeholder,):
        """Retrieve the time coverage from a dictionary of placeholders.

        Args:
            filled_placeholder: A dictionary with placeholders and their
                fillings.

        Returns:
            A tuple of two datetime objects.
        """
        if not filled_placeholder:
            return None

        start_placeholders = {
            p: v for p, v in filled_placeholder.items()
            if p in self._path_start_time_placeholders
        }
        start_date = self._retrieve_timestamp(
            start_placeholders
        )

        end_placeholders = {
            p.lstrip("end_"): v for p, v in filled_placeholder.items()
            if p.startswith("end_")
            and p.lstrip("end_") in self._path_end_time_placeholders
        }
        end_date = self._retrieve_timestamp(
            end_placeholders,
            base=start_date,
        )

        # Sometimes the filename does not explicitly provide the complete
        # end date. Imagine there is only hour and minute given, then day
        # change would not be noticed. Therefore, make sure that the end
        # date is always bigger (later) than the start date.
        if end_date is not None and end_date < start_date:
            end_date += self._path_end_time_overshooting_compensator

        return start_date, end_date

    def _retrieve_timestamp(
            self, filled_placeholder, end=False, base=None):
        """Creates a datetime object from filled placeholders.

        Args:
            filled_placeholder:  A dictionary with placeholders and their
                fillings.
            base: The placeholder might be incomplete, then this is the base
                date that will be updated.

        Returns:
            A dictionary with "year", "month", etc.
        """

        if not filled_placeholder:
            return None

        date_args = {}

        for placeholder, value in filled_placeholder.items():
            if placeholder == "year2":
                # TODO: What should be the threshold that decides whether the
                # TODO: year is 19xx or 20xx?
                if value < self.year2_threshold:
                    date_args["year"] = 2000 + value
                else:
                    date_args["year"] = 1900 + value
            elif placeholder == "millisecond":
                date_args["microsecond"] = value * 1000
            else:
                date_args[placeholder] = value

        if "doy" in filled_placeholder:
            try:
                base = datetime(date_args["year"], 1, 1) \
                       + timedelta(date_args["doy"] - 1)
            except TypeError:
                raise ValueError(
                    "Not enough placeholders for creating {} date!".format(
                        "end" if end else "start"
                    )
                )
            del date_args["doy"]

        if base is None:
            try:
                return datetime(**date_args)
            except TypeError:
                raise ValueError(
                    "Not enough placeholders for creating {} date!".format(
                        "end" if end else "start"
                    )
                )
        else:
            return base.replace(**date_args)

    def save_info_cache(self, filename):
        """ Saves information cache to a file.

        Returns:
            None
        """
        if filename is not None:
            print("Save information cache of {} dataset to {}.".format(
                self.name, filename))
            with open(filename, 'w') as file:
                # We cannot save datetime objects with json directly. We have
                # to convert them to strings first:
                info_cache = [
                    info.to_json_dict()
                    for info in self.info_cache.values()
                ]
                json.dump(info_cache, file)

    @property
    def time_coverage(self):
        """

        Returns:
            The time coverage of the whole dataset (if it is a single file) as
            tuple of datetime objects or (if it is a multi file dataset) the
            fixed time duration of each file as timedelta.

        """
        return self._time_coverage

    @time_coverage.setter
    def time_coverage(self, value):
        """

        Returns:

        """
        if self.single_file:
            if value is None:
                # The default for single file datasets:
                self._time_coverage = [
                    datetime.min,
                    datetime.max
                ]
            else:
                self._time_coverage = [
                    self._to_datetime(value[0]),
                    self._to_datetime(value[1]),
                ]
        elif value is not None:
            self._time_coverage = self._to_timedelta(value)
        else:
            self._time_coverage = None

        # Reset the info cache because some time coverages may change in the
        # future.
        self.info_cache = {}

        return self._time_coverage

    @staticmethod
    def _to_datetime(obj):
        if isinstance(obj, datetime):
            return obj
        else:
            return pd.to_datetime(obj).to_pydatetime()

    @staticmethod
    def _to_timedelta(obj):
        if isinstance(obj, timedelta):
            return obj
        elif isinstance(obj, numbers.Number):
            return timedelta(seconds=int(obj))
        else:
            return pd.to_timedelta(obj).to_pytimedelta()

    def write(self, filename, data, **writing_arguments):
        """Writes content to a file by using the Dataset's file handler.

        If the filename extension is a compression format (such as *zip*,
        etc. look at :func:`typhon.files.is_compression_format` for a list) and
        *Dataset.compress* is set to true, the file will be compressed.

        Notes:
            You need to specify a file handler for this dataset before you
            can use this method.

        Args:
            filename: Path and name of the file where to put the data.
            data: An object that can be stored by the used file handler class.
            **writing_arguments: Additional key word arguments for the
            *write* method of the used file handler class.

        Returns:
            None
        """
        if self.handler is None:
            raise NoHandlerError(
                "Could not write data to the file '{}'! No file handler is "
                "specified!".format(filename))

        if isinstance(filename, FileInfo):
            filename = filename.path

        # The users should not be bothered with creating directories by
        # themselves.
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        if self.compress:
            with typhon.files.compress(filename) as file:
                return self.handler.write(file, data, **writing_arguments)
        else:
            return self.handler.write(filename, data, **writing_arguments)


class DatasetManager(dict):
    def __init__(self, *args, **kwargs):
        """ This manager can hold multiple Dataset objects. You can use it as a
        native dictionary.

        More functionality will be added in future.

        Example:

        .. code-block:: python

            datasets = DatasetManager()

            datasets += Dataset(
                name="images",
                files="path/to/files.png",
            )

            # do something with it
            for name, dataset in datasets["images"].items():
                dataset.find_files(...)

        """
        super(DatasetManager, self).__init__(*args, **kwargs)

    def __iadd__(self, dataset):
        if dataset.name in self:
            warnings.warn(
                "DatasetManager: Overwrite dataset with name '%s'!"
                % dataset.name, RuntimeWarning)

        self[dataset.name] = dataset
        return self

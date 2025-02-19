# This code is part of Qiskit.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
Analysis class for curve fitting.
"""
# pylint: disable=invalid-name

import copy
import dataclasses
import functools
import inspect
import warnings
from abc import ABC
from typing import Dict, List, Tuple, Callable, Union, Optional

import numpy as np
import uncertainties
from uncertainties import unumpy as unp

from qiskit.utils import detach_prefix
from qiskit_experiments.curve_analysis.curve_data import (
    CurveData,
    SeriesDef,
    FitData,
    ParameterRepr,
    FitOptions,
)
from qiskit_experiments.curve_analysis.curve_fit import multi_curve_fit
from qiskit_experiments.curve_analysis.data_processing import multi_mean_xy_data, data_sort
from qiskit_experiments.curve_analysis.visualization import MplCurveDrawer, BaseCurveDrawer
from qiskit_experiments.data_processing import DataProcessor
from qiskit_experiments.data_processing.exceptions import DataProcessorError
from qiskit_experiments.data_processing.processor_library import get_processor
from qiskit_experiments.exceptions import AnalysisError
from qiskit_experiments.framework import (
    BaseAnalysis,
    ExperimentData,
    AnalysisResultData,
    Options,
    AnalysisConfig,
)

PARAMS_ENTRY_PREFIX = "@Parameters_"
DATA_ENTRY_PREFIX = "@Data_"


class CurveAnalysis(BaseAnalysis, ABC):
    """A base class for curve fit type analysis.

    The subclasses can override class attributes to define the behavior of
    data extraction and fitting. This docstring describes how code developers can
    create a new curve fit analysis subclass inheriting from this base class.

    Class Attributes:
        - ``__series__``: A set of data points that will be fit to the same parameters
          in the fit function. If this analysis contains multiple curves,
          the same number of series definitions should be listed. Each series definition
          is a :class:`SeriesDef` element, that may be initialized with

            - ``fit_func``: The function to which the data will be fit.
            - ``filter_kwargs``: Circuit metadata key and value associated with this curve.
              The data points of the curve are extracted from ExperimentData based on
              this information.
            - ``name``: Name of the curve. This is arbitrary data field, but should be unique.
            - ``plot_color``: String color representation of this series in the plot.
            - ``plot_symbol``: String formatter of the scatter of this series in the plot.

        - ``__fixed_parameters__``: A list of parameter names fixed during the fitting.
            These parameters should be provided in some way. For example, you can provide
            them via experiment options or analysis options. Parameter names should be
            used in the ``fit_func`` in the series definition.

        See the Examples below for more details.


    Examples:

        **A fitting for single exponential decay curve**

        In this type of experiment, the analysis deals with a single curve.
        Thus filter_kwargs and series name are not necessary defined.

        .. code-block::

            class AnalysisExample(CurveAnalysis):

                __series__ = [
                    SeriesDef(
                        fit_func=lambda x, p0, p1, p2:
                            exponential_decay(x, amp=p0, lamb=p1, baseline=p2),
                    ),
                ]

        **A fitting for two exponential decay curve with partly shared parameter**

        In this type of experiment, the analysis deals with two curves.
        We need a __series__ definition for each curve, and filter_kwargs should be
        properly defined to separate each curve series.

        .. code-block::

            class AnalysisExample(CurveAnalysis):

                __series__ = [
                    SeriesDef(
                        name="my_experiment1",
                        fit_func=lambda x, p0, p1, p2, p3:
                            exponential_decay(x, amp=p0, lamb=p1, baseline=p3),
                        filter_kwargs={"experiment": 1},
                        plot_color="red",
                        plot_symbol="^",
                    ),
                    SeriesDef(
                        name="my_experiment2",
                        fit_func=lambda x, p0, p1, p2, p3:
                            exponential_decay(x, amp=p0, lamb=p2, baseline=p3),
                        filter_kwargs={"experiment": 2},
                        plot_color="blue",
                        plot_symbol="o",
                    ),
                ]

        In this fit model, we have 4 parameters `p0, p1, p2, p3` and both series share
        `p0` and `p3` as `amp` and `baseline` of the `exponential_decay` fit function.
        Parameter `p1` (`p2`) is only used by `my_experiment1` (`my_experiment2`).
        Both series have same fit function in this example.


        **A fitting for two trigonometric curves with the same parameter**

        In this type of experiment, the analysis deals with two different curves.
        However the parameters are shared with both functions.

        .. code-block::

            class AnalysisExample(CurveAnalysis):

                __series__ = [
                    SeriesDef(
                        name="my_experiment1",
                        fit_func=lambda x, p0, p1, p2, p3:
                            cos(x, amp=p0, freq=p1, phase=p2, baseline=p3),
                        filter_kwargs={"experiment": 1},
                        plot_color="red",
                        plot_symbol="^",
                    ),
                    SeriesDef(
                        name="my_experiment2",
                        fit_func=lambda x, p0, p1, p2, p3:
                            sin(x, amp=p0, freq=p1, phase=p2, baseline=p3),
                        filter_kwargs={"experiment": 2},
                        plot_color="blue",
                        plot_symbol="o",
                    ),
                ]

        In this fit model, we have 4 parameters `p0, p1, p2, p3` and both series share
        all parameters. However, these series have different fit curves, i.e.
        `my_experiment1` (`my_experiment2`) uses the `cos` (`sin`) fit function.


        **A fitting with fixed parameter**

        In this type of experiment, we can provide fixed fit function parameter.
        This parameter should be assigned via analysis options
        and not passed to the fitter function.

        .. code-block::

            class AnalysisExample(CurveAnalysis):

                __series__ = [
                    SeriesDef(
                        fit_func=lambda x, p0, p1, p2:
                            exponential_decay(x, amp=p0, lamb=p1, baseline=p2),
                    ),
                ]

                __fixed_parameters__ = ["p1"]

        You can add arbitrary number of parameters to the class variable
        ``__fixed_parameters__`` from the fit function arguments.
        This parameter should be defined with the fit functions otherwise the analysis
        instance cannot be created. In above example, parameter ``p1`` should be also
        defined in the analysis options. This parameter will be excluded from the fit parameters
        and thus will not appear in the analysis result.

    Notes:
        This CurveAnalysis class provides several private methods that subclasses can override.

        - Customize pre-data processing:
            Override :meth:`~self._format_data`. For example, here you can apply smoothing
            to y values, remove outlier, or apply filter function to the data.
            By default, data is sorted by x values and the measured values at the same
            x value are averaged.

        - Create extra data from fit result:
            Override :meth:`~self._extra_database_entry`. You need to return a list of
            :class:`~qiskit_experiments.framework.analysis_result_data.AnalysisResultData`
            object. This returns an empty list by default.

        - Customize fit quality evaluation:
            Override :meth:`~self._evaluate_quality`. This value will be shown in the
            database. You can determine the quality represented by the predefined string
            "good" or "bad" based on fit result,
            such as parameter uncertainty and reduced chi-squared value.
            This returns ``None`` by default. This means evaluation is not performed.

        - Customize fitting options:
            Override :meth:`~self._generate_fit_guesses`. For example, here you can
            calculate initial guess from experiment data and setup fitter options.

        See docstring of each method for more details.

        Note that other private methods are not expected to be overridden.
        If you forcibly override these methods, the behavior of analysis logic is not well tested
        and we cannot guarantee it works as expected (you may suffer from bugs).
        Instead, you can open an issue in qiskit-experiment github to upgrade this class
        with proper unittest framework.

        https://github.com/Qiskit/qiskit-experiments/issues
    """

    #: List[SeriesDef]: List of mapping representing a data series
    __series__ = list()

    def __init__(self):
        """Initialize data fields that are privately accessed by methods."""
        super().__init__()

        if hasattr(self, "__fixed_parameters__"):
            warnings.warn(
                "The class attribute __fixed_parameters__ has been deprecated and will be removed. "
                "Now this attribute is absorbed in analysis options as fixed_parameters. "
                "This warning will be dropped in v0.4 along with "
                "the support for the deprecated attribute.",
                DeprecationWarning,
                stacklevel=2,
            )
            # pylint: disable=no-member
            self._options.fixed_parameters = {
                p: self.options.get(p, None) for p in self.__fixed_parameters__
            }

        #: List[CurveData]: Processed experiment data set.
        self.__processed_data_set = list()

        #: List[int]: Index of physical qubits
        self._physical_qubits = None

    @classmethod
    def _fit_params(cls) -> List[str]:
        """Return a list of fitting parameters.

        Returns:
            A list of fit parameter names.

        Raises:
            AnalysisError: When series definitions have inconsistent multi-objective fit function.
            ValueError: When fixed parameter name is not used in the fit function.
        """
        fsigs = set()
        for series_def in cls.__series__:
            fsigs.add(inspect.signature(series_def.fit_func))
        if len(fsigs) > 1:
            raise AnalysisError(
                "Fit functions specified in the series definition have "
                "different function signature. They should receive "
                "the same parameter set for multi-objective function fit."
            )

        # remove the first function argument. this is usually x, i.e. not a fit parameter.
        return list(list(fsigs)[0].parameters.keys())[1:]

    @property
    def parameters(self) -> List[str]:
        """Return parameters of this curve analysis."""
        return [s for s in self._fit_params() if s not in self.options.fixed_parameters]

    @property
    def drawer(self) -> BaseCurveDrawer:
        """A short-cut for curve drawer instance."""
        return self._options.curve_plotter

    @classmethod
    def _default_options(cls) -> Options:
        """Return default analysis options.

        Analysis Options:
            curve_plotter (BaseCurveDrawer): A curve drawer instance to visualize
                the analysis result.
            plot_raw_data (bool): Set ``True`` to draw un-formatted data points on canvas.
                This is ``True`` by default.
            plot (bool): Set ``True`` to create figure for fit result.
                This is ``False`` by default.
            curve_fitter (Callable): A callback function to perform fitting with formatted data.
                See :func:`~qiskit_experiments.analysis.multi_curve_fit` for example.
            data_processor (Callable): A callback function to format experiment data.
                This can be a :class:`~qiskit_experiments.data_processing.DataProcessor`
                instance that defines the `self.__call__` method.
            normalization (bool) : Set ``True`` to normalize y values within range [-1, 1].
            p0 (Dict[str, float]): Array-like or dictionary
                of initial parameters.
            bounds (Dict[str, Tuple[float, float]]): Array-like or dictionary
                of (min, max) tuple of fit parameter boundaries.
            x_key (str): Circuit metadata key representing a scanned value.
            result_parameters (List[Union[str, ParameterRepr]): Parameters reported in the
                database as a dedicated entry. This is a list of parameter representation
                which is either string or ParameterRepr object. If you provide more
                information other than name, you can specify
                ``[ParameterRepr("alpha", "\u03B1", "a.u.")]`` for example.
                The parameter name should be defined in the series definition.
                Representation should be printable in standard output, i.e. no latex syntax.
            return_data_points (bool): Set ``True`` to return formatted XY data.
            extra (Dict[str, Any]): A dictionary that is appended to all database entries
                as extra information.
            curve_fitter_options (Dict[str, Any]) Options that are passed to the
                specified curve fitting function.
            fixed_parameters (Dict[str, Any]): Fitting model parameters that are fixed
                during the curve fitting. This should be provided with default value
                keyed on one of the parameter names in the series definition.
        """
        options = super()._default_options()

        options.curve_plotter = MplCurveDrawer()
        options.plot_raw_data = False
        options.plot = True
        options.curve_fitter = multi_curve_fit
        options.data_processor = None
        options.normalization = False
        options.x_key = "xval"
        options.result_parameters = None
        options.return_data_points = False
        options.extra = dict()
        options.curve_fitter_options = dict()
        options.p0 = {}
        options.bounds = {}
        options.fixed_parameters = {}

        return options

    def set_options(self, **fields):
        """Set the analysis options for :meth:`run` method.

        Args:
            fields: The fields to update the options

        Raises:
            KeyError: When removed option ``curve_fitter`` is set.
            TypeError: When invalid drawer instance is provided.
        """
        # TODO remove this in Qiskit Experiments v0.4
        if "curve_plotter" in fields and isinstance(fields["curve_plotter"], str):
            plotter_str = fields["curve_plotter"]
            warnings.warn(
                f"The curve plotter '{plotter_str}' has been deprecated. "
                "The option is replaced with 'MplCurveDrawer' instance. "
                "If this is a loaded analysis, please save this instance again to update option value. "
                "This warning will be removed with backport in Qiskit Experiments 0.4.",
                DeprecationWarning,
                stacklevel=2,
            )
            fields["curve_plotter"] = MplCurveDrawer()

        if "curve_plotter" in fields and not isinstance(fields["curve_plotter"], BaseCurveDrawer):
            plotter_obj = fields["curve_plotter"]
            raise TypeError(
                f"'{plotter_obj.__class__.__name__}' object is not valid curve drawer instance."
            )

        # pylint: disable=no-member
        draw_options = set(self.drawer.options.__dict__.keys()) | {"style"}
        deprecated = draw_options & fields.keys()
        if any(deprecated):
            warnings.warn(
                f"Option(s) {deprecated} have been moved to draw_options and will be removed soon. "
                "Use self.drawer.set_options instead. "
                "If this is a loaded analysis, please save this instance again to update option value. "
                "This warning will be removed with backport in Qiskit Experiments 0.4.",
                DeprecationWarning,
                stacklevel=2,
            )
            draw_options = dict()
            for depopt in deprecated:
                if depopt == "style":
                    for k, v in fields.pop("style").items():
                        draw_options[k] = v
                else:
                    draw_options[depopt] = fields.pop(depopt)
            self.drawer.set_options(**draw_options)

        super().set_options(**fields)

    def _generate_fit_guesses(self, user_opt: FitOptions) -> Union[FitOptions, List[FitOptions]]:
        """Create algorithmic guess with analysis options and curve data.

        Subclasses can override this method.

        Subclass can access to the curve data with ``self._data()`` method.
        If there are multiple series, you can get a specific series by specifying ``series_name``.
        This method returns a ``CurveData`` instance, which is the `dataclass`
        containing x values `.x`, y values `.y`, and  sigma values `.y_err`.

        Subclasses can also access the defined analysis options with the ``self._get_option``.
        For example:

        .. code-block::

            curve_data = self._data(series_name="my_experiment1")

            if self._get_option("my_option1") == "abc":
                param_a_guess = my_guess_function(curve_data.x, curve_data.y, ...)
            else:
                param_a_guess = ...

            user_opt.p0.set_if_empty(param_a=param_a_guess)

        Note that this subroutine can generate multiple fit options.
        If multiple options are provided, the fitter will run multiple times,
        i.e. once for each fit option.
        The result with the best reduced chi-squared value is kept.

        Note that the argument ``user_opt`` is a collection of fitting options (initial guesses,
        boundaries, and extra fitter options) with the user-provided guesses and boundaries.
        The method :meth:`set_if_empty` sets the value of specified parameters of the fit options
        dictionary only if the values of these parameters have not yet been assigned.

        .. code-block::

            opt1 = user_opt.copy()
            opt1.p0.set_if_empty(param_a=3)

            opt2 = user_opt.copy()
            opt2.p0.set_if_empty(param_a=4)

            return [opt1, opt2]

        Note that you can also change fitter options (not only initial guesses and boundaries)
        in each fit options with :meth:`add_extra_options` method.
        This might be convenient to run fitting with multiple fit algorithms
        or different fitting options. By default, this class uses `scipy.curve_fit`
        as the fitter function. See Scipy API docs for more fitting option details.
        See also :py:class:`qiskit_experiments.curve_analysis.curve_data.FitOptions`
        for the behavior of the fit option instance.

        The final fit parameters are decided with the following procedure.

        1. :class:`FitOptions` object is initialized with user options.

        2. Algorithmic guess is generated here and override the default fit options object.

        3. A list of fit options is returned.

        4. Duplicated entries are eliminated.

        5. The fitter optimizes parameters with unique fit options and outputs the chisq value.

        6. The best fit is selected based on the minimum chisq.

        Note that in this method you don't need to worry about the user provided initial guesses
        and boundaries. These values are already assigned in the ``user_opts``.

        Args:
            user_opt: Fit options filled with user provided guess and bounds.

        Returns:
            List of fit options that are passed to the fitter function.
        """

        return user_opt

    def _format_data(self, data: CurveData) -> CurveData:
        """An optional subroutine to perform data pre-processing.

        Subclasses can override this method to apply pre-precessing to data values to fit.

        For example,

        - Apply smoothing to y values to deal with noisy observed values
        - Remove redundant data points (outlier)
        - Apply frequency filter function

        etc...

        By default, the analysis just takes average over the same x values and sort
        data index by the x values in ascending order.

        .. note::

            The data returned by this method should have the label "fit_ready".

        Returns:
            Formatted CurveData instance.
        """
        # take average over the same x value by keeping sigma
        series, xdata, ydata, sigma, shots = multi_mean_xy_data(
            series=data.data_index,
            xdata=data.x,
            ydata=data.y,
            sigma=data.y_err,
            shots=data.shots,
            method="shots_weighted",
        )

        # sort by x value in ascending order
        series, xdata, ydata, sigma, shots = data_sort(
            series=series,
            xdata=xdata,
            ydata=ydata,
            sigma=sigma,
            shots=shots,
        )

        return CurveData(
            label="fit_ready",
            x=xdata,
            y=ydata,
            y_err=sigma,
            shots=shots,
            data_index=series,
        )

    # pylint: disable=unused-argument
    def _extra_database_entry(self, fit_data: FitData) -> List[AnalysisResultData]:
        """Calculate new quantity from the fit result.

        Subclasses can override this method to do post analysis.

        Args:
            fit_data: Fit result.

        Returns:
            List of database entry created from the fit data.
        """
        return []

    def _post_process_fit_result(self, fit_result: FitData) -> FitData:
        """A hook that sub-classes can override to manipulate the result of the fit.

        Args:
            fit_result: A result from the fitting.

        Returns:
            A fit result that might be post-processed.
        """
        return fit_result

    # pylint: disable=unused-argument
    def _evaluate_quality(self, fit_data: FitData) -> Union[str, None]:
        """Evaluate quality of the fit result.

        Subclasses can override this method to do post analysis.

        Args:
            fit_data: Fit result.

        Returns:
            String that represents fit result quality. Usually "good" or "bad".
        """
        return None

    def _extract_curves(
        self, experiment_data: ExperimentData, data_processor: Union[Callable, DataProcessor]
    ):
        """Extract curve data from experiment data.

        This method internally populates two types of curve data.

        - raw_data:

            This is the data directly obtained from the experiment data.
            You can access this data with ``self._data(label="raw_data")``.

        - fit_ready:

            This is the formatted data created by pre-processing defined by
            `self._format_data()` method. This method is implemented by subclasses.
            You can access to this data with ``self._data(label="fit_ready")``.

        If multiple series exist, you can optionally specify ``series_name`` in
        ``self._data`` method to filter data in the target series.

        .. notes::
            The target metadata properties to define each curve entry is described by
            the class attribute __series__ (see `filter_kwargs`).

        Args:
            experiment_data: ExperimentData object to fit parameters.
            data_processor: A callable or DataProcessor instance to format data into numpy array.
                This should take a list of dictionaries and return two tuple of float values,
                that represent a y value and an error of it.
        Raises:
            DataProcessorError: When `x_key` specified in the analysis option is not
                defined in the circuit metadata.
            AnalysisError: When formatted data has label other than fit_ready.
        """
        self.__processed_data_set = list()

        def _is_target_series(datum, **filters):
            try:
                return all(datum["metadata"][key] == val for key, val in filters.items())
            except KeyError:
                return False

        # Extract X, Y, Y_sigma data
        data = experiment_data.data()

        x_key = self.options.x_key
        try:
            xdata = np.asarray([datum["metadata"][x_key] for datum in data], dtype=float)
        except KeyError as ex:
            raise DataProcessorError(
                f"X value key {x_key} is not defined in circuit metadata."
            ) from ex

        if isinstance(data_processor, DataProcessor):
            ydata = data_processor(data)
        else:
            y_nominals, y_stderrs = zip(*map(data_processor, data))
            ydata = unp.uarray(y_nominals, y_stderrs)

        # Store metadata
        metadata = np.asarray([datum["metadata"] for datum in data], dtype=object)

        # Store shots
        shots = np.asarray([datum.get("shots", np.nan) for datum in data])

        # Find series (invalid data is labeled as -1)
        data_index = np.full(xdata.size, -1, dtype=int)
        for idx, series_def in enumerate(self.__series__):
            data_matched = np.asarray(
                [_is_target_series(datum, **series_def.filter_kwargs) for datum in data], dtype=bool
            )
            data_index[data_matched] = idx

        # Store raw data
        raw_data = CurveData(
            label="raw_data",
            x=xdata,
            y=unp.nominal_values(ydata),
            y_err=unp.std_devs(ydata),
            shots=shots,
            data_index=data_index,
            metadata=metadata,
        )
        self.__processed_data_set.append(raw_data)

        # Format raw data
        formatted_data = self._format_data(raw_data)
        if formatted_data.label != "fit_ready":
            raise AnalysisError(f"Not expected data label {formatted_data.label} != fit_ready.")
        self.__processed_data_set.append(formatted_data)

    def _data(
        self,
        series_name: Optional[str] = None,
        label: Optional[str] = "fit_ready",
    ) -> CurveData:
        """Getter for experiment data set.

        Args:
            series_name: Series name to search for.
            label: Label attached to data set. By default it returns "fit_ready" data.

        Returns:
            Filtered curve data set.

        Raises:
            AnalysisError: When requested series or label are not defined.
        """
        # pylint: disable = undefined-loop-variable
        for data in self.__processed_data_set:
            if data.label == label:
                break
        else:
            raise AnalysisError(f"Requested data with label {label} does not exist.")

        if series_name is None:
            return data

        for idx, series_def in enumerate(self.__series__):
            if series_def.name == series_name:
                locs = data.data_index == idx
                return CurveData(
                    label=label,
                    x=data.x[locs],
                    y=data.y[locs],
                    y_err=data.y_err[locs],
                    shots=data.shots[locs],
                    data_index=idx,
                    metadata=data.metadata[locs] if data.metadata is not None else None,
                )

        raise AnalysisError(f"Specified series {series_name} is not defined in this analysis.")

    @property
    def _num_qubits(self) -> int:
        return len(self._physical_qubits)

    def _run_analysis(
        self, experiment_data: ExperimentData
    ) -> Tuple[List[AnalysisResultData], List["pyplot.Figure"]]:
        #
        # 1. Parse arguments
        #

        # Update all fit functions in the series definitions if fixed parameter is defined.
        assigned_params = self.options.fixed_parameters

        if assigned_params:
            # Check if all parameters are assigned.
            if any(v is None for v in assigned_params.values()):
                raise AnalysisError(
                    f"Unassigned fixed-value parameters for the fit "
                    f"function {self.__class__.__name__}."
                    f"All values of fixed-parameters, i.e. {assigned_params}, "
                    "must be provided by the analysis options to run this analysis."
                )

            # Override series definition with assigned fit functions.
            assigned_series = []
            for series_def in self.__series__:
                dict_def = dataclasses.asdict(series_def)
                dict_def["fit_func"] = functools.partial(series_def.fit_func, **assigned_params)
                del dict_def["signature"]
                assigned_series.append(SeriesDef(**dict_def))
            self.__series__ = assigned_series

        # get experiment metadata
        try:
            self._physical_qubits = experiment_data.metadata["physical_qubits"]
        except KeyError:
            pass

        #
        # 2. Setup data processor
        #

        # If no data processor was provided at run-time we infer one from the job
        # metadata and default to the data processor for averaged classified data.
        data_processor = self.options.data_processor

        if not data_processor:
            data_processor = get_processor(experiment_data, self.options)

        if isinstance(data_processor, DataProcessor) and not data_processor.is_trained:
            # Qiskit DataProcessor instance. May need calibration.
            data_processor.train(data=experiment_data.data())

        # Initialize fit figure canvas
        if self.options.plot:
            self.drawer.initialize_canvas()

        #
        # 3. Extract curve entries from experiment data
        #
        self._extract_curves(experiment_data=experiment_data, data_processor=data_processor)

        # TODO remove _data method dependency in follow-up
        #  self.__processed_data_set will be removed from instance.

        # Draw raw data
        if self.options.plot and self.options.plot_raw_data:
            for s in self.__series__:
                raw_data = self._data(label="raw_data", series_name=s.name)
                self.drawer.draw_raw_data(
                    x_data=raw_data.x,
                    y_data=raw_data.y,
                    ax_index=s.canvas,
                )

        # Draw formatted data
        if self.options.plot:
            for s in self.__series__:
                curve_data = self._data(label="fit_ready", series_name=s.name)
                self.drawer.draw_formatted_data(
                    x_data=curve_data.x,
                    y_data=curve_data.y,
                    y_err_data=curve_data.y_err,
                    name=s.name,
                    ax_index=s.canvas,
                    color=s.plot_color,
                    marker=s.plot_symbol,
                )

        #
        # 4. Run fitting
        #
        formatted_data = self._data(label="fit_ready")

        # Generate algorithmic initial guesses and boundaries
        default_fit_opt = FitOptions(
            parameters=self.parameters,
            default_p0=self.options.p0,
            default_bounds=self.options.bounds,
            **self.options.curve_fitter_options,
        )

        fit_options = self._generate_fit_guesses(default_fit_opt)
        if isinstance(fit_options, FitOptions):
            fit_options = [fit_options]

        # Run fit for each configuration
        fit_results = []
        for fit_opt in set(fit_options):
            try:
                fit_result = self.options.curve_fitter(
                    funcs=[series_def.fit_func for series_def in self.__series__],
                    series=formatted_data.data_index,
                    xdata=formatted_data.x,
                    ydata=formatted_data.y,
                    sigma=formatted_data.y_err,
                    **fit_opt.options,
                )
                fit_results.append(fit_result)
            except AnalysisError:
                # Some guesses might be too far from the true parameters and may thus fail.
                # We ignore initial guesses that fail and continue with the next fit candidate.
                pass

        # Find best value with chi-squared value
        if len(fit_results) == 0:
            warnings.warn(
                "All initial guesses and parameter boundaries failed to fit the data. "
                "Please provide better initial guesses or fit parameter boundaries.",
                UserWarning,
            )
            # at least return raw data points rather than terminating
            fit_result = None
        else:
            fit_result = sorted(fit_results, key=lambda r: r.reduced_chisq)[0]
            fit_result = self._post_process_fit_result(fit_result)

        #
        # 5. Create database entry
        #
        analysis_results = []
        if fit_result:
            # pylint: disable=assignment-from-none
            quality = self._evaluate_quality(fit_data=fit_result)

            fit_models = {
                series_def.name: series_def.model_description or "no description"
                for series_def in self.__series__
            }

            # overview entry
            analysis_results.append(
                AnalysisResultData(
                    name=PARAMS_ENTRY_PREFIX + self.__class__.__name__,
                    value=[p.nominal_value for p in fit_result.popt],
                    chisq=fit_result.reduced_chisq,
                    quality=quality,
                    extra={
                        "popt_keys": fit_result.popt_keys,
                        "dof": fit_result.dof,
                        "covariance_mat": fit_result.pcov,
                        "fit_models": fit_models,
                        **self.options.extra,
                    },
                )
            )

            # output special parameters
            result_parameters = self.options.result_parameters
            if result_parameters:
                for param_repr in result_parameters:
                    if isinstance(param_repr, ParameterRepr):
                        p_name = param_repr.name
                        p_repr = param_repr.repr or param_repr.name
                        unit = param_repr.unit
                    else:
                        p_name = param_repr
                        p_repr = param_repr
                        unit = None

                    fit_val = fit_result.fitval(p_name)
                    if unit:
                        metadata = copy.copy(self.options.extra)
                        metadata["unit"] = unit
                    else:
                        metadata = self.options.extra

                    result_entry = AnalysisResultData(
                        name=p_repr,
                        value=fit_val,
                        chisq=fit_result.reduced_chisq,
                        quality=quality,
                        extra=metadata,
                    )
                    analysis_results.append(result_entry)

            # add extra database entries
            analysis_results.extend(self._extra_database_entry(fit_result))

        if self.options.return_data_points:
            # save raw data points in the data base if option is set (default to false)
            raw_data_dict = dict()
            for series_def in self.__series__:
                series_data = self._data(series_name=series_def.name, label="raw_data")
                raw_data_dict[series_def.name] = {
                    "xdata": series_data.x,
                    "ydata": series_data.y,
                    "sigma": series_data.y_err,
                }
            raw_data_entry = AnalysisResultData(
                name=DATA_ENTRY_PREFIX + self.__class__.__name__,
                value=raw_data_dict,
                extra={
                    "x-unit": self.drawer.options.xval_unit,
                    "y-unit": self.drawer.options.yval_unit,
                },
            )
            analysis_results.append(raw_data_entry)

        # Draw fit results if fitting succeeded
        if self.options.plot and fit_result:
            for s in self.__series__:
                interp_x = np.linspace(*fit_result.x_range, 100)

                params = {}
                for fitpar in s.signature:
                    if fitpar in self.options.fixed_parameters:
                        params[fitpar] = self.options.fixed_parameters[fitpar]
                    else:
                        params[fitpar] = fit_result.fitval(fitpar)

                y_data_with_uncertainty = s.fit_func(interp_x, **params)
                y_mean = unp.nominal_values(y_data_with_uncertainty)
                y_std = unp.std_devs(y_data_with_uncertainty)
                # Draw fit line
                self.drawer.draw_fit_line(
                    x_data=interp_x,
                    y_data=y_mean,
                    ax_index=s.canvas,
                    color=s.plot_color,
                )
                # Draw confidence intervals with different n_sigma
                sigmas = unp.std_devs(y_data_with_uncertainty)
                if np.isfinite(sigmas).all():
                    for n_sigma, alpha in self.drawer.options.plot_sigma:
                        self.drawer.draw_confidence_interval(
                            x_data=interp_x,
                            y_ub=y_mean + n_sigma * y_std,
                            y_lb=y_mean - n_sigma * y_std,
                            ax_index=s.canvas,
                            alpha=alpha,
                            color=s.plot_color,
                        )

            # Draw fitting report
            report_description = ""
            for res in analysis_results:
                if isinstance(res.value, (float, uncertainties.UFloat)):
                    report_description += f"{analysis_result_to_repr(res)}\n"
            report_description += r"Fit $\chi^2$ = " + f"{fit_result.reduced_chisq: .4g}"
            self.drawer.draw_fit_report(description=report_description)

        # Output figure
        if self.options.plot:
            self.drawer.format_canvas()
            figures = [self.drawer.figure]
        else:
            figures = []

        return analysis_results, figures

    @classmethod
    def from_config(cls, config: Union[AnalysisConfig, Dict]) -> "CurveAnalysis":
        # For backward compatibility. This will be removed in v0.4.

        instance = super().from_config(config)

        # When fixed param value is hard-coded as options. This is deprecated data structure.
        loaded_opts = instance.options.__dict__

        # pylint: disable=no-member
        deprecated_fixed_params = {
            p: loaded_opts[p] for p in instance.parameters if p in loaded_opts
        }
        if any(deprecated_fixed_params):
            warnings.warn(
                "Fixed parameter value should be defined in options.fixed_parameters as "
                "a dictionary values, rather than a standalone analysis option. "
                "Please re-save this experiment to be loaded after deprecation period. "
                "This warning will be dropped in v0.4 along with "
                "the support for the deprecated fixed parameter options.",
                DeprecationWarning,
                stacklevel=2,
            )
            new_fixed_params = instance.options.fixed_parameters
            new_fixed_params.update(deprecated_fixed_params)
            instance.set_options(fixed_parameters=new_fixed_params)

        return instance


def is_error_not_significant(
    val: Union[float, uncertainties.UFloat],
    fraction: float = 1.0,
    absolute: Optional[float] = None,
) -> bool:
    """Check if the standard error of given value is not significant.

    Args:
        val: Input value to evaluate. This is assumed to be float or ufloat.
        fraction: Valid fraction of the nominal part to its standard error.
            This function returns ``False`` if the nominal part is
            smaller than the error by this fraction.
        absolute: Use this value as a threshold if given.

    Returns:
        ``True`` if the standard error of given value is not significant.
    """
    if isinstance(val, float):
        return True

    threshold = absolute if absolute is not None else fraction * val.nominal_value
    if np.isnan(val.std_dev) or val.std_dev < threshold:
        return True

    return False


def analysis_result_to_repr(result: AnalysisResultData) -> str:
    """A helper function to create string representation from analysis result data object.

    Args:
        result: Analysis result data.

    Returns:
        String representation of the data.
    """
    if not isinstance(result.value, (float, uncertainties.UFloat)):
        return AnalysisError(f"Result data {result.name} is not a valid fit parameter data type.")

    unit = result.extra.get("unit", None)

    def _format_val(value):
        # Return value with unit with prefix, i.e. 1000 Hz -> 1 kHz.
        if unit:
            try:
                val, val_prefix = detach_prefix(value, decimal=3)
            except ValueError:
                val = value
                val_prefix = ""
            return f"{val: .3g}", f" {val_prefix}{unit}"
        if np.abs(value) < 1e-3 or np.abs(value) > 1e3:
            return f"{value: .4e}", ""
        return f"{value: .4g}", ""

    if isinstance(result.value, float):
        # Only nominal part
        n_repr, n_unit = _format_val(result.value)
        value_repr = n_repr + n_unit
    else:
        # Nominal part
        n_repr, n_unit = _format_val(result.value.nominal_value)

        # Standard error part
        if result.value.std_dev is not None and np.isfinite(result.value.std_dev):
            s_repr, s_unit = _format_val(result.value.std_dev)
            if n_unit == s_unit:
                value_repr = f" {n_repr} \u00B1 {s_repr}{n_unit}"
            else:
                value_repr = f" {n_repr + n_unit} \u00B1 {s_repr + s_unit}"
        else:
            value_repr = n_repr + n_unit

    return f"{result.name} = {value_repr}"

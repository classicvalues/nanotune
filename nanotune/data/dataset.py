import copy
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
import numpy.typing as npt
import numpy as np
import qcodes as qc
from qcodes.dataset.data_set import DataSet
import scipy.fftpack as fp
import scipy.signal as sg
import xarray as xr
from scipy.ndimage import gaussian_filter

import nanotune as nt

LABELS = list(nt.config["core"]["labels"].keys())
default_coord_names = {
    "voltage": ["voltage_x", "voltage_y"],
    "frequency": ["frequency_x", "frequency_y"],
}
old_readout_methods = {
    'dc_current': 'transport',
    'dc_sensor': 'sensing',
}
default_readout_methods = nt.config["core"]["readout_methods"]
logger = logging.getLogger(__name__)


class Dataset:
    """Emulates the QCoDeS dataset and adds data post-processing
    functionalities.

    It loads QCodeS data to an xarray dataset and applies previously measured
    normalization constants. It keeps both raw and normalized data in separate
    xarray dataset attributes. Fourier frequencies or Gaussian filtered data
    is computed as well.

    Attributes:
        qc_run_id: Captured run ID in of QCodeS dataset.
        db_name: database name
        db_folder: folder containing database
        normalization_tolerances: if normalization constants are not correct,
            this is the range within which the normalized signal is still
            accepted without throwing an error. Useful when a lot of noise is
            present.
        exp_id: QCoDeS experiment ID.
        guid: QCoDeS data GUID.
        qc_parameters: list of QCoDeS `ParamSpec` of parameters swept and
            measured.
        ml_label: list of machine learning labels. Empty if not labelled.
        dimensions: dimensionality of the measurement.
        readout_methods: mapping readout type (transport, sensing or rf) to
            the corresponding QCoDeS parameter.
        raw_data: xarray dataset containing un-processed data, as returned by
            `qc_dataset.to_xarray_dataset()`.
        data: xarray dataset containing normalized data and whose keys have
            been renamed to "standard" readout methods defined in `Readout`.
        power_spectrum: xarray dataset containing Fourier frequencies whose
            keys have been renamed to "standard" readout methods defined in
            `Readout`.
        filtered_data: xarray dataset containing data to which a Gaussian
            filter has been applied. Keys have been renamed to "standard"
            readout methods defined in `Readout`.
    """

    def __init__(
        self,
        qc_run_id: int,
        db_name: Optional[str] = None,
        db_folder: Optional[str] = None,
        normalization_tolerances: Tuple[float, float] = (-0.1, 1.1),
    ) -> None:

        if db_folder is None:
            db_folder = nt.config["db_folder"]
        self.db_folder = db_folder

        if db_name is None:
            self.db_name, _ = nt.get_database()
        else:
            nt.set_database(db_name, db_folder=db_folder)
            self.db_name = db_name

        self.qc_run_id = qc_run_id
        self._snapshot: Dict[str, Any] = {}
        self._nt_metadata: Dict[str, Any] = {}
        self._normalization_constants: Dict[str, List[float]] = {}
        self._normalization_tolerances = normalization_tolerances

        self.exp_id: int
        self.guid: str
        self.qc_parameters: List[qc.Parameter]
        self.ml_label: List[str]
        self.dimensions: Dict[str, int] = {}
        self.readout_methods: Dict[str, str] = {}

        self.raw_data: xr.Dataset = xr.Dataset()
        self.data: xr.Dataset = xr.Dataset()
        self.power_spectrum: xr.Dataset = xr.Dataset()
        self.filtered_data: xr.Dataset = xr.Dataset()

        self.from_qcodes_dataset()
        self.prepare_filtered_data()
        self.compute_power_spectrum()

    @property
    def snapshot(self) -> Dict[str, Any]:
        """"""
        return self._snapshot

    @property
    def nt_metadata(self) -> Dict[str, Any]:
        """"""
        return self._nt_metadata

    @property
    def normalization_constants(self) -> Dict[str, List[float]]:
        """"""
        return self._normalization_constants

    @property
    def features(self) -> Dict[str, Dict[str, Any]]:
        """"""
        if "features" in self._nt_metadata:
            return self._nt_metadata["features"]
        else:
            return {}

    def get_plot_label(
        self, readout_method: str, axis: int, power_spect: bool = False
    ) -> str:
        """"""
        curr_dim = self.dimensions[readout_method]
        assert curr_dim >= axis
        data = self.data[readout_method]

        if axis == 0 or (axis == 1 and curr_dim > 1):
            if power_spect:
                lbl = default_coord_names["frequency"][axis].replace("_", " ")
            else:
                lbl = data[default_coord_names["voltage"][axis]].attrs["label"]
        else:
            if power_spect:
                lbl = f"power spectrum {data.name}".replace("_", " ")
            else:
                ut = data.unit
                lbl = f"{data.name} [{ut}]".replace("_", " ")

        return lbl

    def from_qcodes_dataset(self):
        """ Load data from qcodes dataset """
        qc_dataset = qc.load_by_run_spec(captured_run_id=self.qc_run_id)
        self.exp_id = qc_dataset.exp_id
        self.guid = qc_dataset.guid
        self.qc_parameters = qc_dataset.get_parameters()

        self.raw_data = qc_dataset.to_xarray_dataset()

        self._load_metadata_from_qcodes(qc_dataset)
        self._prep_qcodes_data()

    def _load_metadata_from_qcodes(self, qc_dataset: DataSet):
        self._normalization_constants = {
            key: [0.0, 1.0] for key in ["transport", "rf", "sensing"]
        }
        self._snapshot = {}
        self._nt_metadata = {}

        try:
            self._snapshot = json.loads(qc_dataset.get_metadata("snapshot"))
        except (RuntimeError, TypeError):
            pass
        try:
            self._nt_metadata = json.loads(qc_dataset.get_metadata(nt.meta_tag))
        except (RuntimeError, TypeError):
            pass

        try:
            nm = self._nt_metadata["normalization_constants"]
            for old_key, new_key in old_readout_methods.items():
                if old_key in nm.keys():
                    nm[new_key] = nm.pop(old_key)
            self._normalization_constants.update(nm)
        except KeyError:
            pass

        try:
            self.device_name = self._nt_metadata["device_name"]
        except KeyError:
            logger.warning("No device name specified.")
            self.device_name = "noname_device"

        try:
            self.readout_methods = self._nt_metadata["readout_methods"]
        except KeyError:
            logger.warning("No readout method specified.")
            self.readout_methods = {}
        if not isinstance(self.readout_methods, dict) or not self.readout_methods:
            if isinstance(self.readout_methods, str):
                methods = [self.readout_methods]
            elif isinstance(self.readout_methods, list):
                methods = self.readout_methods
            else:
                # we assume the default order of readout methods if nothing
                # else is specified
                methods = default_readout_methods[: len(self.raw_data)]
            read_params = [str(it) for it in list(self.raw_data.data_vars)]
            self.readout_methods = dict(zip(methods, read_params))

        quality = qc_dataset.get_metadata("good")
        if quality is not None:
            self.quality = int(quality)
        else:
            self.quality = None

        self.ml_label = []
        for label in LABELS:
            if label != "good":
                lbl = qc_dataset.get_metadata(label)
                if lbl is None:
                    lbl = 0
                if int(lbl) == 1:
                    self.ml_label.append(label)

    def _prep_qcodes_data(self, rename: bool = True):
        """Prepares nanotune-ready data.
        Renames xarray dataset keys, applies normalization constants and
        loads machine learning labels.
        """
        self.data = self.raw_data.interpolate_na()
        if rename:
            self._rename_xarray_variables()

        for r_meth, r_param in self.readout_methods.items():
            self.dimensions[r_meth] = len(self.raw_data[r_param].dims)
            nrm_dt = self._normalize_data(self.raw_data[r_param].values, r_meth)
            self.data[r_meth].values = nrm_dt

            for vi, vr in enumerate(self.raw_data[r_param].depends_on):
                coord_name = default_coord_names["voltage"][vi]
                lbl = self._nt_label(r_param, vr)
                self.data[r_meth][coord_name].attrs["label"] = lbl

        self.data = self.data.sortby(list(self.data.coords), ascending=True)

    def _nt_label(self, readout_paramter, var) -> str:
        lbl = self.raw_data[readout_paramter][var].attrs["label"]
        unit = self.raw_data[readout_paramter][var].attrs["unit"]
        return f"{lbl} [{unit}]"

    def _rename_xarray_variables(self):
        """Renames xarray dataset keys."""
        new_names = {old: new for new, old in self.readout_methods.items()}
        new_names_all = copy.deepcopy(new_names)
        for old_n in new_names.keys():
            for c_i, old_crd in enumerate(self.raw_data[old_n].depends_on):
                new_names_all[old_crd] = default_coord_names["voltage"][c_i]

        self.data = self.data.rename(new_names_all)

    def _normalize_data(self,
        signal: npt.NDArray[np.float64],
        signal_type,
    ) -> npt.NDArray[np.float64]:
        """Applies normalization constants."""
        minv = self.normalization_constants[signal_type][0]
        maxv = self.normalization_constants[signal_type][1]

        normalized_sig = (signal - minv) / (maxv - minv)
        min_tol, max_tol = self._normalization_tolerances
        if np.max(normalized_sig) > max_tol or np.min(normalized_sig) < min_tol:
            msg = (
                "Dataset {}: ".format(self.qc_run_id),
                "Wrong normalization constant",
            )
            logger.warning(msg)
        return normalized_sig

    def prepare_filtered_data(self):
        """Applies a Gaussian filter and saves the result under the
        'filtered_data' attribute."""
        self.filtered_data = self.data.copy(deep=True)
        for read_meth in self.readout_methods:
            smooth = gaussian_filter(
                self.filtered_data[read_meth].values, sigma=2
            )
            self.filtered_data[read_meth].values = smooth

    def compute_1D_power_spectrum(
        self,
        readout_method: str,
    ) -> xr.DataArray:
        """Computes Fourier frequencies of a 1D measurement.
        Data is detrended before the Fourier transformation is applied.
        """
        c_name = default_coord_names["voltage"][0]
        voltage_x = self.data[readout_method][c_name].values

        xv = np.unique(voltage_x)
        signal = self.data[readout_method].values.copy()
        signal = sg.detrend(signal, axis=0)

        frequencies_res = fp.fft(signal)
        frequencies_res = np.abs(fp.fftshift(frequencies_res)) ** 2

        fx = fp.fftshift(fp.fftfreq(frequencies_res.shape[0], d=xv[1] - xv[0]))

        freq_xar = xr.DataArray(
            frequencies_res,
            coords=[(default_coord_names["frequency"][0], fx)],
        )
        return freq_xar

    def compute_2D_power_spectrum(
        self,
        readout_method: str,
    ) -> xr.DataArray:
        """Computes Fourier frequencies of a 2D measurement.
        Data is detrended before the Fourier transformation is applied.
        """
        c_name_x = default_coord_names["voltage"][0]
        c_name_y = default_coord_names["voltage"][1]
        voltage_x = self.data[readout_method][c_name_x].values
        voltage_y = self.data[readout_method][c_name_y].values
        signal = self.data[readout_method].values.copy()

        xv = np.unique(voltage_x)
        yv = np.unique(voltage_y)
        signal = signal.copy()

        signal = sg.detrend(signal, axis=0)
        signal = sg.detrend(signal, axis=1)

        frequencies_res = fp.fft2(signal)
        frequencies_res = np.abs(fp.fftshift(frequencies_res)) ** 2

        fx_1d = fp.fftshift(fp.fftfreq(frequencies_res.shape[0], d=xv[1] - xv[0]))
        fy_1d = fp.fftshift(fp.fftfreq(frequencies_res.shape[1], d=yv[1] - yv[0]))

        freq_xar = xr.DataArray(
            frequencies_res,
            coords=[
                (default_coord_names["frequency"][0], fx_1d),
                (default_coord_names["frequency"][1], fy_1d),
            ],
        )

        return freq_xar

    def compute_power_spectrum(self):
        """Computes Fourier frequencies of a measurement.
        Each readout method, i.e. measured trace within the dataset, is treated
        seperately.
        The data is detrended to eliminate DC component, but no high pass
        filter applied as we do not want to accidentally remove
        information contained in low frequencies.
        """
        self.power_spectrum = xr.Dataset()

        for readout_method in self.readout_methods.keys():
            if self.dimensions[readout_method] == 1:
                freq_xar = self.compute_1D_power_spectrum(readout_method)
            elif self.dimensions[readout_method] == 2:
                freq_xar = self.compute_2D_power_spectrum(readout_method)
            else:
                raise NotImplementedError

            self.power_spectrum[readout_method] = freq_xar
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

import sys
sys.path.append('..')
from dataset import *

import numpy as np
import blosc
import dicom

from preprocessing.auxiliaries import resize_chunk_numba
from preprocessing.auxiliaries import resize_patient_numba
from preprocessing.auxiliaries import get_filter_patient

from preprocessing.mip import image_XIP as XIP
from preprocessing.crop import return_black_border_array as rbba

INPUT_FOLDER = '/notebooks/data/MRT/nci/'
BLOSC_STORAGE = '/notebooks/data/MRT/blosc_preprocessed/'
AIR_HU = -2000


def unpack_blosc(blosc_dir_path):
    """
    unpacker of blosc files
    """
    with open(blosc_dir_path, mode='rb') as f:
        packed = f.read()

    return blosc.unpack_array(packed)


class BatchIterator(object):

    """
    iterator for Batch

    instance of Batch contains concatenated (along 0-axis) patients
        in Batch.data

    consecutive "floors" with numbers from Batch.lower_bounds[i]
        to Batch.upper_bounds[i] belong to patient i


    iterator for Batch iterates over patients
        i-th iteration returns view on i-th patient's data
    """

    def __init__(self, batch):
        self._batch = batch
        self._patient_index = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._patient_index >= self._batch._lower_bounds.shape[0]:
            raise StopIteration
        else:
            lower = self._batch._lower_bounds[self._patient_index]
            upper = self._batch._upper_bounds[self._patient_index]
            return_value = self._batch._data[lower:upper, :, :]
            self._patient_index += 1
            return return_value


class BatchCt(Batch):

    """
    class for storing batch of CT(computed tomography) 3d-scans.
        Derived from base class Batch


    Attrs:
        1. index: array of PatientIDs. Usually, PatientIDs are strings
        2. _data: 3d-array of stacked scans along number_of_slices axis
        3. _lower_bounds: 1d-array of first floors for each patient
        4. _upper_bounds: 1d, last floors
        5. history: list of preprocessing operations applied to self
            when method with @action-decorator is called
            info about call should be appended to history

        6. _patient_index_path: index (usually, patientID) -> storage for this patient
                storage is either directory (dicom-case) or file: .blk for blosc
                or .raw for mhd
        7. _patient_index_number: index -> patient's order in batch
                of use for iterators and indexation


    Important methods:
        1. __init__(self, index):
            basic initialization of patient
            in accordance with Batch.__init__
            given base class Batch

        2. load(self, all_patients_paths,
                btype='dicom'):
            builds skyscraper of patients given 
            correspondance patient index -> storage
            and type of data to build from
            returns self

        2. resize(self, new_sizes, order, num_threads):
            transform the shape of all patients to new_sizes
            method is spline iterpolation(order = order)
            the function is multithreaded in num_threads
            returns self

        3. dump(self, path)
            create a dump of the batch
            in the path-folder
            returns self

        4. get_filter(self, erosion_radius=7, num_threads=8)
            returns binary-mask for lungs segmentation
            the larger erosion_radius
            the lesser the resulting lungs will be
            * returns mask, not self 

        5. segment(self, erosion_radius=2, num_threads=8)
            segments using mask from get_filter()
            that is, sets to hu = -2000 of pixels outside mask
            changes self, returns self

    """

    @action
    def load(self, all_patients_paths,
             btype='dicom'):
        """
        builds batch of patients

        args:
            all_patients_paths - paths to files (dicoms/mhd/blosc)
                dict-correspondance patient -> storage
                self.index has to be subset of
                all_patients_paths.keys()
            btype - type of data. 
                Can be 'dicom'|'blosc'|'raw'

        Dicom example:

            # initialize batch for storing batch of 3 patients
            # with following IDs
            ind = ['1ae34g90', '3hf82s76', '2ds38d04']
            batch = BatchCt(ind)

            # initialize dictionary Patient index -> storage
            dicty = {'1ae34g90': './data/DICOM/1ae34g90', 
                     '3hf82s76': './data/DICOM/3hf82s76', 
                     '2ds38d04': './data/DICOM/2ds38d04'}
            batch.load(dicty, btype='dicom')

        Blosc example:
            ind = ['1ae34g90', '3hf82s76', '2ds38d04']
            batch = BatchCt(ind)

            # initialize dictionary Patient index -> storage
            dicty = {'1ae34g90': './data/DICOM/1ae34g90/data.blk', 
                     '3hf82s76': './data/DICOM/3hf82s76/data.blk', 
                     '2ds38d04': './data/DICOM/2ds38d04/data.blk'}
            batch.load(dicty, btype='blosc')


        ***to do: rewrite initialization with asynchronicity
        """

        # define dictionaries for indexation
        # index (patient name) -> path for storing his data
        self._patient_index_path = {patient:
                                    all_patients_paths[patient] for patient in self.index}
        self._patient_index_number = {self.index[i]:
                                      i for i in range(len(self.index))}

        # read, prepare and put 3d-scans in list
        # depending on the input type
        if btype == 'dicom':
            list_of_arrs = self._make_dicom()
        elif btype == 'blosc':
            list_of_arrs = self._make_blosc()
        elif btype == 'raw':
            list_of_arrs = self._make_raw()
        else:
            raise TypeError("Incorrect type of batch source")

        # concatenate scans and initialize patient bounds
        self._initialize_data_and_bounds(list_of_arrs)

        # add info in self.history
        info = {}
        info['method'] = 'load'
        info['params'] = {}
        self.history.append(info)

        return self

    def __init__(self, index):
        """
        common part of initialization from all formats:
            -execution of Batch construction
            -initialization of all attrs
            -creation of empty lists and arrays

        attrs:
            index - ndarray of indices
            dtype is likely to be string    
        """

        super().__init__(index)

        self._data = None

        self._upper_bounds = np.array([], dtype=np.int32)
        self._lower_bounds = np.array([], dtype=np.int32)

        self._patient_index_path = dict()
        self._patient_index_number = dict()

        self._crop_centers = np.array([], dtype=np.int32)
        self._crop_sizes = np.array([], dtype=np.int32)

        self.history = []

    def _make_dicom(self):
        """
        read, prepare and put 3d-scans in list
            given that self contains paths to dicoms in
            self._patient_index_path

            NOTE: 
                Important operations performed here:
                - conversion to hu using meta from dicom-scans
                - 
        """

        list_of_arrs = []
        for patient in self.index:
            patient_folder = self._patient_index_path[patient]

            list_of_dicoms = [dicom.read_file(os.path.join(patient_folder, s))
                              for s in os.listdir(patient_folder)]

            list_of_dicoms.sort(key=lambda x: int(x.ImagePositionPatient[2]),
                                reverse=True)
            intercept_pat = list_of_dicoms[0].RescaleIntercept
            slope_pat = list_of_dicoms[0].RescaleSlope

            patient_data = np.stack([s.pixel_array
                                     for s in list_of_dicoms]).astype(np.int16)

            patient_data[patient_data == AIR_HU] = 0

            if slope_pat != 1:
                patient_data = slope_pat * patient_data.astype(np.float64)
                patient_data = patient_data.astype(np.int16)

            patient_data += np.int16(intercept_pat)
            list_of_arrs.append(patient_data)
        return list_of_arrs

    def _make_blosc(self):
        """
        read, prepare and put 3d-scans in list
            given that self contains paths to blosc in
            self._patient_index_path

            *no conversion to hu here
        """
        list_of_arrs = [unpack_blosc(self._patient_index_path[patient])
                        for patient in self.index]

        return list_of_arrs

    def _make_raw(self):
        """
        read, prepare and put 3d-scans in list
            given that self contains paths to raw (see itk library) in
            self._patient_index_path

            *no conversion to hu here
        """
        list_of_arrs = [sitk.GetArrayFromImage(sitk.ReadImage(self._patient_index_path[patient]))
                        for patient in self.index]
        return list_of_arrs

    def _initialize_data_and_bounds(self, list_of_arrs):
        """
        put the list of 3d-scans into self._data
        fill in self._upper_bounds and 
            self._lower_bounds accordingly 

        args:
            self
            list_of_arrs: list of 3d-scans
        """
        # make 3d-skyscraper from list of 3d-scans
        self._data = np.concatenate(list_of_arrs, axis=0)

        # set floors for each patient
        list_of_lengths = [len(a) for a in list_of_arrs]
        self._upper_bounds = np.cumsum(np.array(list_of_lengths))
        self._lower_bounds = np.insert(self._upper_bounds, 0, 0)[:-1]

    def __iter__(self):
        return BatchIterator(self)

    def __len__(self):
        return self._lower_bounds.shape[0]

    def __getitem__(self, index):
        """
        indexation of patients by []

        args:
            self
            index - can be either number (int) of patient
                         in self from [0,..,len(self.index) - 1]
                    or index from self.index 
                    (here we assume the latter is always 
                    string)
        """
        if isinstance(index, int):
            if index < self._lower_bounds.shape[0] and index >= 0:
                lower = self._lower_bounds[index]
                upper = self._upper_bounds[index]
                return self._data[lower:upper, :, :]
            else:
                raise IndexError(
                    "Index of patient in the batch is out of range")

        elif isinstance(index, str):
            lower = self._lower_bounds[self._patient_index_number[index]]
            upper = self._upper_bounds[self._patient_index_number[index]]
            return self._data[lower:upper, :, :]
        else:
            raise ValueError("Wrong type of index for batch object")

    @property
    def crop_centers(self):
        if not self._crop_centers:
            self._crop_params_patients()
        return self._crop_centers

    @property
    def crop_sizes(self):
        if not self._crop_sizes:
            self._crop_params_patients()
        return self._crop_sizes

    @property
    def patient_names_paths(self):
        """
        Return list of tuples containing patient name and his data directory
        """
        return list(self._patient_index_path.items())

    @property
    def patient_indices(self):
        """
        Return ordered list of patient names.
        """
        return list(self._patient_index_number.keys())

    @property
    def patient_paths(self):
        """
        Return ordered list of patients' data directories.
        """
        return list(self._patient_index_path.values())

    @property
    def patient_names_indexes(self):
        """
        Return list of tuples containing patient index 
            and his number in batch.
        """
        return list(self._patient_index_number.items())

    def _crop_params_patients(self, num_threads=8):
        with ThreadPoolExecutor(max_workers=num_threads) as executor:

            threads = [executor.submit(rbba, pat) for pat in self]
            crop_array = np.array([t.result() for t in threads])

            self._crop_centers = crop_array[:, :, 2]
            self._crop_sizes = crop_array[:, :, : 2]

    @action
    def make_XIP(self, step: int = 2, depth: int = 10,
                 func: str = 'max', projection: str = 'axial',
                 num_threads: int = 4, verbose: bool = False) -> "Batch":
        """
        This function takes 3d picture represented by np.ndarray image,
        start position for 0-axis index, stop position for 0-axis index,
        step parameter which represents the step across 0-axis and, finally,
        depth parameter which is associated with the depth of slices across
        0-axis made on each step for computing MEAN, MAX, MIN
        depending on func argument.
        Possible values for func are 'max', 'min' and 'avg'.
        Notice that 0-axis in this annotation is defined in accordance with
        projection argument which may take the following values: 'axial',
        'coroanal', 'sagital'.
        Suppose that input 3d-picture has axis associations [z, x, y], then
        axial projection doesn't change the order of axis and 0-axis will
        be correspond to 0-axis of the input array.
        However in case of 'coronal' and 'sagital' projections the source tensor
        axises will be transposed as [x, z, y] and [y, z, x]
        for 'coronal' and 'sagital' projections correspondingly.
        """
        args_list = []
        for l, u in zip(self._lower_bounds, self._upper_bounds):

            args_list.append(dict(image=self._data,
                                  start=l,
                                  stop=u,
                                  step=step,
                                  depth=depth,
                                  func=func,
                                  projection=projection,
                                  verbose=verbose))

        upper_bounds = None
        lower_bounds = None
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            list_of_lengths = []

            mip_patients = list(executor.map(lambda x: XIP(**x), args_list))
            for patient_mip_array in mip_patients:
                axis_null_size = patient_mip_array.shape[0]
                list_of_lengths.append(axis_null_size)

            upper_bounds = np.cumsum(np.array(list_of_lengths))
            lower_bounds = np.insert(upper_bounds, 0, 0)[:-1]

        return Batch.from_array(np.concatenate(mip_patients, axis=0),
                                lower_bounds, upper_bounds, self.history)

    @action
    def resize(self, num_slices_new=200, num_x_new=400,
               num_y_new=400, num_threads=8, order=3):
        """
        performs resize (change of shape) of each CT-scan in the batch.
            When called from Batch, changes Batch
            returns self

            params: (num_slices_new, num_x_new, num_y_new) sets new shape
            num_threads: number of threads used (degree of parallelism)
            order: the order of interpolation (<= 5)
                large value can improve precision, but also slows down the computaion



        example: Batch = Batch.resize(num_slices_new=128, num_x_new=256,
                                      num_y_new=256, num_threads=25)
        """

        # save the result into result_stacked
        result_stacked = np.zeros((len(self) *
                                   num_slices_new, num_x_new, num_y_new))

        # define array of args
        args = []
        for num_pat in range(len(self)):

            args_dict = {'chunk': self._data,
                         'start_from': self._lower_bounds[num_pat],
                         'end_from': self._upper_bounds[num_pat],
                         'num_slices_new': num_slices_new,
                         'num_x_new': num_x_new,
                         'num_y_new': num_y_new,
                         'res': result_stacked,
                         'start_to': num_pat * num_slices_new}

            args.append(args_dict)

        # print(args)
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            for arg in args:
                executor.submit(resize_patient_numba, **arg)

        # add info to history
        info = {}
        info['method'] = 'resize'
        info['params'] = {'num_slices_new': num_slices_new,
                          'num_x_new': num_x_new,
                          'num_y_new': num_y_new,
                          'num_threads': num_threads,
                          'order': order}

        self.history.append(info)

        # change data
        self._data = result_stacked

        # change lower/upper bounds
        cur_pat_num = len(self)
        self._lower_bounds = np.arange(cur_pat_num) * num_slices_new
        self._upper_bounds = np.arange(1, cur_pat_num + 1) * num_slices_new

        return self

    @action
    def get_filter(self, erosion_radius=7, num_threads=8):
        """
        we multithread
            computation of filter for lungs segmentation


        remember, our patient version of segmentaion has signature
            get_filter_patient(chunk, start_from, end_from, res,
                                      start_to, erosion_radius = 7):

        """
        # we put filter into array
        result_stacked = np.zeros_like(self._data)

        # define array of args
        args = []
        for num_pat in range(len(self)):

            args_dict = {'chunk': self._data,
                         'start_from': self._lower_bounds[num_pat],
                         'end_from': self._upper_bounds[num_pat],
                         'res': result_stacked,
                         'start_to': self._lower_bounds[num_pat],
                         'erosion_radius': erosion_radius}
            args.append(args_dict)

        # run threads and put the fllter into result_stacked
        # print(args)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            for arg in args:
                executor.submit(get_filter_patient, **arg)

        # return filter
        return result_stacked

    @action
    def segment(self, erosion_radius=2, num_threads=8):
        """
        lungs segmenting function
            changes self

        sets hu of every pixes outside lungs
            to -2000

        example: 
            batch = batch.segment(erosion_radius=4, num_threads=20)
        """
        # get filter with specified params
        # reverse it and set not-lungs to -2000

        lungs = self.get_filter(erosion_radius=erosion_radius,
                                num_threads=num_threads)
        self._data = self._data * lungs

        result_filter = 1 - lungs
        result_filter *= -2000

        # apply filter to self.data
        self._data += result_filter

        # add info about segmentation to history
        info = {}
        info['method'] = 'segmentation'

        info['params'] = {'erosion_radius': erosion_radius,
                          'num_threads': num_threads}

        self.history.append(info)

        return self

    def get_axial_slice(self, person_number, slice_height):
        """
        get axial slice (e.g., for plots)

        args: person_number - can be either 
            number of person in the batch
            or index of the person
                whose axial slice we need

        slice_height: e.g. 0.7 means that we take slice with number
            int(0.7 * number of slices for person)

        example: patch = batch.get_axial_slice(5, 0.6)
                 patch = batch.get_axial_slice(self.index[5], 0.6)
                 # here self.index[5] usually smth like 'a1de03fz29kf6h2'

        """
        margin = int(slice_height * self[person_number].shape[0])

        patch = self[person_number][margin, :, :]
        return patch

    @action
    def dump(self, dump_path):
        """
        dump on specified path
            create folder corresponding to each patient

        example: 
            # initialize batch and load data
            ind = ['1ae34g90', '3hf82s76', '2ds38d04']
            batch = BatchCt(ind)

            batch.load(...)

            batch.dump('./data/blosc_preprocessed')
            # the command above creates files

            # ./data/blosc_preprocessed/1ae34g90/data.blk
            # ./data/blosc_preprocessed/3hf82s76/data.blk
            # ./data/blosc_preprocessed/2ds38d04/data.blk
        """

        for pat_index in self.index:
            # view on patient data
            pat_data = self[pat_index]

            # pack the data
            packed = blosc.pack_array(pat_data, cname='zstd', clevel=1)

            # remove directory if exists
            if os.path.exists(os.path.join(dump_path, pat_index)):
                shutil.rmtree(os.path.join(dump_path, pat_index))

            # put blosc on disk
            os.makedirs(os.path.join(dump_path, pat_index))

            with open(os.path.join(dump_path,
                                   pat_index, 'data.blk'),
                      mode='wb') as file:
                file.write(packed)

        # add info in self.history
        info = {}
        info['method'] = 'dump'
        info['params'] = {'dump_path': dump_path}
        self.history.append(info)

        return self

if __name__ == "__main__":
    pass

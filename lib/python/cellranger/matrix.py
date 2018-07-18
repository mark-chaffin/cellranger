#!/usr/bin/env python
#
# Copyright (c) 2015 10X Genomics, Inc. All rights reserved.
#
from collections import OrderedDict
import copy
import h5py as h5
import itertools
import json
import numpy as np
import os
import pandas as pd
pd.set_option("compute.use_numexpr", False)
import shutil
import tables
import scipy.sparse as sp_sparse
import tenkit.safe_json as tk_safe_json
import cellranger.h5_constants as h5_constants
import cellranger.library_constants as lib_constants
from cellranger.feature_ref import FeatureReference
import cellranger.utils as cr_utils
import cellranger.io as cr_io
import cellranger.sparse as cr_sparse

HDF5_COMPRESSION = 'gzip'
# Number of elements per chunk. Here, 1 MiB / (12 bytes)
HDF5_CHUNK_SIZE = 80000

DEFAULT_DATA_DTYPE = 'int32'

# some helper functions from stats
def sum_sparse_matrix(matrix, axis=0):
    '''Sum a sparse matrix along an axis.'''
    return np.squeeze(np.asarray(matrix.sum(axis=axis)))

def top_n(array, n):
    '''Retrieve the N largest elements and their positions in a numpy ndarray.
    Args:
       array (numpy.ndarray): Array
       n (int): Number of elements

    Returns:
       list of tuple of (int, x): Tuples are (original index, value).
    '''
    indices = np.argpartition(array, -n)[-n:]
    indices = indices[np.argsort(array[indices])]
    return zip(indices, array[indices])

MATRIX_H5_FILETYPE = u'matrix'
MATRIX_H5_VERSION_KEY = u'version'
MATRIX_H5_VERSION = 2

class CountMatrixView(object):
    """Supports summing a sliced CountMatrix w/o copying the whole thing"""
    def __init__(self, matrix, feature_indices=None, bc_indices=None):
        self.feature_mask = np.ones(matrix.features_dim, dtype='bool')
        self.bc_mask = np.ones(matrix.bcs_dim, dtype='bool')
        self.matrix = matrix

        if feature_indices is not None:
            self.feature_mask.fill(False)
            self.feature_mask[np.asarray(feature_indices)] = True
        if bc_indices is not None:
            self.bc_mask.fill(False)
            self.bc_mask[np.asarray(bc_indices)] = True

        self._update_feature_ref()

    @property
    def bcs_dim(self): return np.count_nonzero(self.bc_mask)

    def _copy(self):
        """Return a copy of this view"""
        view = CountMatrixView(self.matrix)
        view.bc_mask = np.copy(self.bc_mask)
        view.feature_mask = np.copy(self.feature_mask)
        view._update_feature_ref()
        return view

    def view(self):
        """Return a copy of this view"""
        return self._copy()

    def sum(self, axis=None):
        """Sum across an axis."""
        return cr_sparse.sum_masked(self.matrix.m, self.feature_mask, self.bc_mask, axis=axis)

    def count_ge(self, axis, threshold):
        """Count number of elements >= X over an axis"""
        return cr_sparse.count_ge_masked(self.matrix.m, self.feature_mask, self.bc_mask, threshold, axis)

    def select_barcodes(self, indices):
        """Select a subset of barcodes (by index in the original matrix) and return the resulting view"""
        view = self._copy()
        mask = np.bincount(indices, minlength=len(view.bc_mask)).astype('bool')
        mask[mask > 1] = 1
        view.bc_mask &= mask
        return view

    def select_barcodes_by_seq(self, barcode_seqs):
        return self.select_barcodes([self.matrix.bc_to_int(bc) for bc in barcode_seqs])

    def select_barcodes_by_gem_group(self, gem_group):
        return self.select_barcodes_by_seq(
            [bc for bc in self.matrix.bcs if gem_group == cr_utils.split_barcode_seq(bc)[1]])

    def _update_feature_ref(self):
        """Make the feature reference consistent with the feature mask"""
        indices = np.flatnonzero(self.feature_mask)
        self.feature_ref = FeatureReference(feature_defs=[self.matrix.feature_ref.feature_defs[i] for i in indices],
                                            all_tag_keys=self.matrix.feature_ref.all_tag_keys)

    def select_features(self, indices):
        """Select a subset of features and return the resulting view."""
        view = self._copy()
        mask = np.bincount(indices, minlength=len(view.feature_mask)).astype('bool')
        mask[mask > 1] = 1
        view.feature_mask &= mask
        view._update_feature_ref()
        return view

    def select_features_by_genome(self, genome):
        """Select the subset of gene-expression features for genes in a specific genome"""
        indices = []
        for feature in self.matrix.feature_ref.feature_defs:
            if feature.feature_type == lib_constants.DEFAULT_LIBRARY_TYPE:
                if feature.tags['genome'] == genome:
                    indices.append(feature.index)
        return self.select_features(indices)

    def select_features_by_type(self, feature_type):
        """Select the subset of features with a particular feature type (e.g. "Gene Expression")"""
        indices = []
        for feature in self.matrix.feature_ref.feature_defs:
            if feature.feature_type == feature_type:
                indices.append(feature.index)
        return self.select_features(indices)

    def get_genomes(self):
        return CountMatrix._get_genomes_from_feature_ref(self.feature_ref)

    def bcs_to_ints(self, bcs):
        # Only works when we haven't masked barcodes.
        if np.count_nonzero(self.bc_mask) != self.matrix.bcs_dim:
            raise NotImplementedError('Calling bcs_to_ints on a barcode-sliced matrix view is unimplemented')
        return self.matrix.bcs_to_ints(bcs)

    def ints_to_bcs(self, bc_ints):
        sliced_bc_ints = np.flatnonzero(self.bc_mask)
        orig_bc_ints = sliced_bc_ints[np.asarray(bc_ints)]
        return [self.matrix.bcs[i] for i in orig_bc_ints]

    def int_to_feature_id(self, i):
        return self.feature_ref.feature_defs[i].id

    def get_shape(self):
        """Return the shape of the sliced matrix"""
        return (np.count_nonzero(self.feature_mask), np.count_nonzero(self.bc_mask))

    def get_num_nonzero(self):
        """Return the number of nonzero entries in the sliced matrix"""
        return self.count_ge(axis=None, threshold=1)

    def get_counts_per_bc(self):
        return self.sum(axis=0)

class CountMatrix(object):
    def __init__(self, feature_ref, bcs, matrix):
        # Features (genes, CRISPR gRNAs, antibody barcodes, etc.)
        self.feature_ref = feature_ref
        self.features_dim = len(feature_ref.feature_defs)
        self.feature_ids_map = { f.id: f.index for f in feature_ref.feature_defs }

        # Cell barcodes
        self.bcs = list(bcs)
        self.bcs_dim = len(self.bcs)
        self.bcs_map = {bc:i for i, bc in enumerate(self.bcs)}

        self.m = matrix

    def view(self):
        """Return a view on this matrix"""
        return CountMatrixView(self)

    @classmethod
    def empty(cls, feature_ref, bcs, dtype=DEFAULT_DATA_DTYPE):
        '''Create an empty matrix.'''
        matrix = sp_sparse.lil_matrix((len(feature_ref.feature_defs), len(bcs)), dtype=dtype)
        return cls(feature_ref=feature_ref, bcs=bcs, matrix=matrix)

    def feature_id_to_int(self, feature_id):
        if feature_id not in self.feature_ids_map:
            raise KeyError("Specified feature ID not found in matrix: %s" % feature_id)
        return self.feature_ids_map[feature_id]

    def feature_ids_to_ints(self, feature_ids):
        return sorted([self.feature_id_to_int(fid) for fid in feature_ids])

    def feature_id_to_name(self, feature_id):
        return self.feature_id_to_int(feature_id)

    def int_to_feature_id(self, i):
        return self.feature_ref.feature_defs[i].id

    def int_to_feature_name(self, i):
        return self.feature_ref.feature_defs[i].name

    def bc_to_int(self, bc):
        if bc not in self.bcs_map:
            raise KeyError("Specified barcode not found in matrix: %s" % bc)
        return self.bcs_map[bc]

    def bcs_to_ints(self, bcs):
        return sorted([self.bc_to_int(bc) for bc in bcs])

    def int_to_bc(self, j):
        return self.bcs[j]

    def ints_to_bcs(self, jj):
        return [self.int_to_bc(j) for j in jj]

    def add(self, feature_id, bc, value=1):
        '''Add a count.'''
        i, j = self.feature_id_to_int(feature_id), self.bc_to_int(bc)
        self.m[i,j] += value

    def get(self, feature_id, bc):
        i, j = self.feature_id_to_int(feature_id), self.bc_to_int(bc)
        return self.m[i,j]

    def merge(self, other):
        '''Merge this matrix with another CountMatrix'''
        assert self.features_dim == other.features_dim
        self.m += other.m

    def save_dense_csv(self, filename):
        '''Save this matrix to a dense CSV file.'''
        dense_cm = pd.DataFrame(self.m.toarray(),
                                index=[f.id for f in self.feature_ref.feature_defs],
                                columns=self.bcs)
        dense_cm.to_csv(filename, index=True, header=True)

    def save_h5_file(self, filename, extra_attrs={}):
        '''Save this matrix to an HDF5 file.'''
        with h5.File(filename, 'w') as f:
            f.attrs[h5_constants.H5_FILETYPE_KEY] = MATRIX_H5_FILETYPE
            f.attrs[MATRIX_H5_VERSION_KEY] = MATRIX_H5_VERSION

            # Set optional top-level attributes
            for (k,v) in extra_attrs.iteritems():
                cr_io.set_hdf5_attr(f, k, v)

            group = f.create_group('matrix')
            self.save_h5_group(group)


    def save_h5_group(self, group):
        '''Save this matrix to an HDF5 (h5py) group.'''
        self.tocsc()

        # Save the feature reference
        feature_ref_group = group.create_group(h5_constants.H5_FEATURE_REF_ATTR)
        self.feature_ref.to_hdf5(feature_ref_group)

        # Store barcode sequences as array of ASCII strings
        cr_io.create_hdf5_string_dataset(group, h5_constants.H5_BCS_ATTR, self.bcs)

        for attr, dtype in h5_constants.H5_MATRIX_ATTRS.iteritems():
            arr = np.array(getattr(self.m, attr), dtype=dtype)
            group.create_dataset(attr, data=arr,
                                 chunks=(HDF5_CHUNK_SIZE,),
                                 maxshape=(None,),
                                 compression=HDF5_COMPRESSION)

    @staticmethod
    def load_dims(group):
        '''Load the matrix shape from an HDF5 group'''
        (rows, cols) = group[h5_constants.H5_MATRIX_SHAPE_ATTR][:]
        entries = len(group[h5_constants.H5_MATRIX_DATA_ATTR])
        return (rows, cols, entries)

    @staticmethod
    def load_dims_from_h5(filename):
        '''Load the matrix shape from an HDF5 file'''
        with h5.File(filename, 'r') as f:
            return CountMatrix.load_dims(f['matrix'])

    @staticmethod
    def get_mem_gb_from_matrix_dim(num_barcodes, nonzero_entries):
        ''' Estimate memory usage of loading a matrix. '''
        matrix_mem_gb = float(nonzero_entries) / h5_constants.NUM_MATRIX_ENTRIES_PER_MEM_GB
        # We store a list and a dict of the whitelist. Based on empirical obs.
        matrix_mem_gb += float(num_barcodes) / h5_constants.NUM_MATRIX_BARCODES_PER_MEM_GB

        return h5_constants.MATRIX_MEM_GB_MULTIPLIER * round(np.ceil(matrix_mem_gb))

    @staticmethod
    def get_mem_gb_from_group(group):
        '''Estimate memory usage from an HDF5 group.'''
        _, num_bcs, nonzero_entries = CountMatrix.load_dims(group)
        return CountMatrix.get_mem_gb_from_matrix_dim(num_bcs, nonzero_entries)

    @staticmethod
    def get_mem_gb_from_matrix_h5(filename):
        '''Estimate memory usage from an HDF5 file.'''
        with h5.File(filename, 'r') as f:
            return CountMatrix.get_mem_gb_from_group(f['matrix'])

    @classmethod
    def load(cls, group):
        '''Load from an HDF5 group.'''
        feature_ref = CountMatrix.load_feature_ref_from_h5_group(group)

        bcs = cls.load_bcs_from_h5_group(group)

        shape = group[h5_constants.H5_MATRIX_SHAPE_ATTR][:]
        data = group[h5_constants.H5_MATRIX_DATA_ATTR][:]
        indices = group[h5_constants.H5_MATRIX_INDICES_ATTR][:]
        indptr = group[h5_constants.H5_MATRIX_INDPTR_ATTR][:]

        # Check to make sure indptr increases monotonically (to catch overflow bugs)
        assert np.all(np.diff(indptr)>=0)

        matrix = sp_sparse.csc_matrix((data, indices, indptr), shape=shape)

        return cls(feature_ref=feature_ref, bcs=bcs, matrix=matrix)

    @classmethod
    def load_chunk(cls, group, col_start, col_end):
        '''Load a submatrix from a CountMatrix HDF5 group

        Load a submatrix specified by the given column (barcode) range from an h5 group
        Args:
            group (h5py.Group): Group to load from.
            col_start (int): 0-based starting column index.
            col_end (int) - Exclusive ending column index.
        Returns:
            scipy.sparse.csc_matrix
        '''

        # Check bounds
        shape = getattr(group, h5_constants.H5_MATRIX_SHAPE_ATTR).read()
        assert col_start >= 0 and col_start < shape[1]
        assert col_end >= 0 and col_end <= shape[1]

        # Load features and barcodes
        feature_ref = CountMatrix.load_feature_ref_from_h5_group(group)
        bcs = CountMatrix.load_bcs_from_h5_group(group)[col_start:col_end]

        # Get views into full matrix
        data = getattr(group, h5_constants.H5_MATRIX_DATA_ATTR)
        indices = getattr(group, h5_constants.H5_MATRIX_INDICES_ATTR)
        indptr = getattr(group, h5_constants.H5_MATRIX_INDPTR_ATTR)

        # Determine extents of selected columns
        ind_start = indptr[col_start]
        if col_end < len(indptr)-1:
            # Last index (end-exclusive) is the start of the next column
            ind_end = indptr[col_end]
        else:
            # Last index is the last index in the matrix
            ind_end = len(data)

        chunk_data = data[ind_start:ind_end]
        chunk_indices = indices[ind_start:ind_end]
        chunk_indptr = np.append(indptr[col_start:col_end], ind_end) - ind_start
        chunk_shape = (shape[0], col_end - col_start)

        matrix = sp_sparse.csc_matrix((chunk_data, chunk_indices, chunk_indptr), shape=chunk_shape)

        return cls(feature_ref=feature_ref, bcs=bcs, matrix=matrix)

    @staticmethod
    def load_bcs_from_h5_group(group):
        '''Load just the barcode sequences from an h5.'''
        return cr_io.read_hdf5_string_dataset(group[h5_constants.H5_BCS_ATTR])

    @staticmethod
    def load_feature_ref_from_h5_group(group):
        '''Load just the FeatureRef from an h5.'''
        feature_group = group[h5_constants.H5_FEATURE_REF_ATTR]
        return FeatureReference.from_hdf5(feature_group)

    def tolil(self):
        if type(self.m) is not sp_sparse.lil_matrix:
            self.m = self.m.tolil()

    def tocoo(self):
        if type(self.m) is not sp_sparse.coo_matrix:
            self.m = self.m.tocoo()

    def tocsc(self):
        # Convert from lil to csc matrix for efficiency when analyzing data
        if type(self.m) is not sp_sparse.csc_matrix:
            self.m = self.m.tocsc()

    def select_nonzero_axes(self):
        '''Select axes with nonzero sums.

        Returns:
            (CountMatrix, np.array of int, np.array of int):
                New count matrix, non-zero bc indices, feat indices
        '''

        new_mat = copy.deepcopy(self)

        nonzero_bcs = np.flatnonzero(new_mat.get_counts_per_bc())
        if self.bcs_dim > len(nonzero_bcs):
            new_mat = new_mat.select_barcodes(nonzero_bcs)

        nonzero_features = np.flatnonzero(new_mat.get_counts_per_feature())
        if new_mat.features_dim > len(nonzero_features):
            new_mat = new_mat.select_features(nonzero_features)

        return new_mat, nonzero_bcs, nonzero_features

    def select_barcodes(self, indices):
        '''Select a subset of barcodes and return the resulting CountMatrix.'''
        return CountMatrix(feature_ref=self.feature_ref,
                           bcs=[self.bcs[i] for i in indices],
                           matrix=self.m[:, indices])

    def select_barcodes_by_seq(self, barcode_seqs):
        return self.select_barcodes([self.bc_to_int(bc) for bc in barcode_seqs])

    def select_barcodes_by_gem_group(self, gem_group):
        return self.select_barcodes_by_seq(
            [bc for bc in self.bcs if gem_group == cr_utils.split_barcode_seq(bc)[1]])

    def select_features(self, indices):
        '''Select a subset of features and return the resulting matrix.'''
        feature_ref = FeatureReference(feature_defs=[self.feature_ref.feature_defs[i] for i in indices],
                                       all_tag_keys=self.feature_ref.all_tag_keys)

        return CountMatrix(feature_ref=feature_ref,
                           bcs=self.bcs,
                           matrix=self.m[indices, :])

    def select_features_by_genome(self, genome):
        '''Select the subset of gene-expression features for genes in a specific genome'''
        indices = []
        for feature in self.feature_ref.feature_defs:
            if feature.feature_type == lib_constants.DEFAULT_LIBRARY_TYPE:
                if feature.tags['genome'] == genome:
                    indices.append(feature.index)
        return self.select_features(indices)

    def select_features_by_type(self, feature_type):
        '''Select the subset of features with a particular feature type (e.g. "Gene Expression")'''
        indices = []
        for feature in self.feature_ref.feature_defs:
            if feature.feature_type == feature_type:
                indices.append(feature.index)
        return self.select_features(indices)


    @staticmethod
    def _get_genomes_from_feature_ref(feature_ref):
        genomes = OrderedDict()
        for feature in feature_ref.feature_defs:
            if feature.feature_type == lib_constants.DEFAULT_LIBRARY_TYPE:
                genomes[feature.tags['genome']] = True
        return genomes.keys()

    def get_genomes(self):
        '''Get a list of the distinct genomes represented by my gene expression features'''
        return CountMatrix._get_genomes_from_feature_ref(self.feature_ref)

    @staticmethod
    def get_genomes_from_h5(filename):
        '''Get a list of the distinct genomes from a matrix HDF5 file'''
        with h5.File(filename, 'r') as f:
            feature_ref = CountMatrix.load_feature_ref_from_h5_group(f['matrix'])
            return CountMatrix._get_genomes_from_feature_ref(feature_ref)

    def get_unique_features_per_bc(self):
        return sum_sparse_matrix(self.m[self.m > 0], axis=0)

    def get_counts_per_bc(self):
        return sum_sparse_matrix(self.m, axis=0)

    def get_counts_per_feature(self):
        return sum_sparse_matrix(self.m, axis=1)

    def get_numbcs_per_feature(self):
        return sum_sparse_matrix(self.m > 0, axis=1)

    def get_top_bcs(self, cutoff):
        reads_per_bc = self.get_counts_per_bc()
        index = max(0, min(reads_per_bc.size, cutoff) - 1)
        value = sorted(reads_per_bc, reverse=True)[index]
        return np.nonzero(reads_per_bc >= value)[0]


    def save_mex(self, base_dir, save_features_func):
        '''Save in Matrix Market Exchange format.'''
        self.tocoo()

        cr_io.makedirs(base_dir, allow_existing=True)

        out_matrix_fn = os.path.join(base_dir, 'matrix.mtx')
        out_barcodes_fn = os.path.join(base_dir, 'barcodes.tsv')

        # This method only supports an integer matrix.
        assert self.m.dtype in ['uint32', 'int32', 'uint64', 'int64']
        assert type(self.m) == sp_sparse.coo.coo_matrix

        rows, cols = self.m.shape
        # Header fields in the file
        rep = 'coordinate'
        field = 'integer'
        symmetry = 'general'
        comment=''

        with open(out_matrix_fn, 'wb') as stream:
            # write initial header line
            stream.write(np.compat.asbytes('%%MatrixMarket matrix {0} {1} {2}\n'.format(rep, field, symmetry)))

            # write comments
            for line in comment.split('\n'):
                stream.write(np.compat.asbytes('%%%s\n' % (line)))

            # write shape spec
            stream.write(np.compat.asbytes('%i %i %i\n' % (rows, cols, self.m.nnz)))
            # write row, col, val in 1-based indexing
            for r, c, d in itertools.izip(self.m.row+1, self.m.col+1, self.m.data):
                stream.write(np.compat.asbytes(("%i %i %i\n" % (r, c, d))))

        # both GEX and ATAC provide an implementation of this in respective feature_ref.py
        save_features_func(self.feature_ref, base_dir)

        with open(out_barcodes_fn, 'w') as f:
            for bc in self.bcs:
                f.write(bc + '\n')

    @staticmethod
    def load_h5_file(filename):
        with h5.File(filename, 'r') as f:
            if h5_constants.H5_FILETYPE_KEY not in f.attrs or \
               f.attrs[h5_constants.H5_FILETYPE_KEY] != MATRIX_H5_FILETYPE:
                raise ValueError('HDF5 file is not a valid matrix HDF5 file.')

            if MATRIX_H5_VERSION_KEY in f.attrs:
                version = f.attrs[MATRIX_H5_VERSION_KEY]
            else:
                version = 1

            if version > MATRIX_H5_VERSION:
                raise ValueError('Matrix HDF5 file format version (%d) is a newer version that is not supported by this version of the software.' % version)
            if version < MATRIX_H5_VERSION:
                raise ValueError('Matrix HDF5 file format version (%d) is an older version that is no longer supported.' % version)

            if 'matrix' not in f.keys():
                raise ValueError('Could not find the "matrix" group inside the matrix HDF5 file.')

            return CountMatrix.load(f['matrix'])

    @staticmethod
    def count_cells_from_h5(filename):
        # NOTE - this double-counts doublets.
        with h5.File(filename, 'r') as f:
            _, bcs, _ = CountMatrix.load_dims(f['matrix'])
            return bcs

    @staticmethod
    def load_chemistry_from_h5(filename):
        with tables.open_file(filename, 'r') as f:
            try:
                chemistry = f.get_node_attr('/', h5_constants.H5_CHEMISTRY_DESC_KEY)
            except AttributeError:
                chemistry = "Unknown"
        return chemistry

    def filter_barcodes(self, bcs_per_genome):
        '''Return CountMatrix containing only the specified barcodes.
        Args:
            bcs_per_genome (dict of str to list): Maps genome to cell-associated barcodes.
        Returns:
            CountMatrix w/ the specified barcodes'''

        # Union all the cell-associated barcodes
        bcs = sorted(list(reduce(lambda a,x: a | set(x), bcs_per_genome.itervalues(), set())))
        return self.select_barcodes_by_seq(bcs)

    def report_summary_json(self, filename, summary_json_paths, barcode_summary_h5_path,
                            recovered_cells, cell_bc_seqs):
        '''summary_json_paths: paths to summary jsons containing total_reads and *_conf_mapped_reads_frac
            barcode_summary_h5_path: path to barcode summary h5 file
        '''
        d = self.report(summary_json_paths,
                        barcode_summary_h5_path=barcode_summary_h5_path,
                        recovered_cells=recovered_cells,
                        cell_bc_seqs=cell_bc_seqs)
        with open(filename, 'w') as f:
            json.dump(tk_safe_json.json_sanitize(d), f, indent=4, sort_keys=True)

    @staticmethod
    def h5_path(base_path):
        return os.path.join(base_path, "hdf5", "matrices.hdf5")

def merge_matrices(h5_filenames):
    matrix = None
    for h5_filename in h5_filenames:
        if matrix is None:
            matrix = CountMatrix.load_h5_file(h5_filename)
        else:
            other = CountMatrix.load_h5_file(h5_filename)
            matrix.merge(other)
    if matrix is not None:
        matrix.tocsc()
    return matrix

def concatenate_mtx(mtx_list, out_mtx):
    if len(mtx_list) == 0:
        return

    with open(out_mtx, 'w') as out_file:
        # write header
        with open(mtx_list[0], 'r') as in_file:
            out_file.write(in_file.readline())
            out_file.write(in_file.readline())
            (genes, bcs, data) = map(int, in_file.readline().rstrip().split())
        for in_mtx in mtx_list[1:]:
            with open(in_mtx, 'r') as in_file:
                in_file.readline()
                in_file.readline()
                (_, _, mo_data) = map(int, in_file.readline().rstrip().split())
                data += mo_data
        out_file.write(' '.join(map(str, [genes, bcs, data])) + '\n')

        # write data
        for in_mtx in mtx_list:
            with open(in_mtx, 'r') as in_file:
                for i in range(3):
                    in_file.readline()
                shutil.copyfileobj(in_file, out_file)

def make_matrix_attrs_count(sample_id, gem_groups, chemistry):
    matrix_attrs = make_library_map_count(sample_id, gem_groups)
    matrix_attrs[h5_constants.H5_CHEMISTRY_DESC_KEY] = chemistry
    return matrix_attrs

def get_matrix_attrs(filename):
    '''Get matrix metadata attributes from an HDF5 file'''
    # TODO: Consider moving these to the 'matrix' key instead of the root group
    attrs = {}
    with h5.File(filename, 'r') as f:
        for key in h5_constants.H5_METADATA_ATTRS:
            val = f.attrs.get(key)
            if val is not None:
                if np.isscalar(val) and hasattr(val, 'item'):
                    # Coerce numpy scalars to python types.
                    # In particular, force np.unicode_ to unicode (python2)
                    #    or str (python 3)
                    attrs[key] = val.item()
                else:
                    attrs[key] = val
    return attrs

def make_library_map_count(sample_id, gem_groups):
    # store gem group mapping for use by Cell Loupe
    unique_gem_groups = sorted(set(gem_groups))
    library_map = {
        h5_constants.H5_LIBRARY_ID_MAPPING_KEY: np.array([sample_id]*len(unique_gem_groups), dtype=str),
        h5_constants.H5_ORIG_GEM_GROUP_MAPPING_KEY: np.array(unique_gem_groups, dtype=int)
    }
    return library_map

def make_library_map_aggr(gem_group_index):
    # store gem group mapping for use by Cell Loupe
    library_ids = []
    original_gem_groups = []
    # Sort numerically by new gem group
    for ng, (lid, og) in sorted(gem_group_index.iteritems(), key=lambda pair: int(pair[0])):
        library_ids.append(lid)
        original_gem_groups.append(og)
    library_map = {
        h5_constants.H5_LIBRARY_ID_MAPPING_KEY: np.array(library_ids, dtype=str),
        h5_constants.H5_ORIG_GEM_GROUP_MAPPING_KEY: np.array(original_gem_groups, dtype=int)
    }
    return library_map

def get_gem_group_index(matrix_h5):
    with tables.open_file(matrix_h5, mode = 'r') as f:
        try:
            library_ids = f.get_node_attr('/', h5_constants.H5_LIBRARY_ID_MAPPING_KEY)
            original_gem_groups = f.get_node_attr('/', h5_constants.H5_ORIG_GEM_GROUP_MAPPING_KEY)
        except AttributeError:
            return None
    library_map = {}
    for ng, (lid, og) in enumerate(zip(library_ids, original_gem_groups), start=1):
        library_map[ng] = (lid, og)
    return library_map

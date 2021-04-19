import os
import random
import logging
import numpy as np
import math
from eight_mile.utils import str2bool
from baseline.utils import exporter

__all__ = []
export = exporter(__all__)
logger = logging.getLogger('baseline')

@export
class DataFeed:
    """Data collection that, when iterated, produces an epoch of data

    This class manages producing a dataset to the trainer, by iterating an epoch and producing
    a single step at a time.  The data can be shuffled per epoch, if requested, otherwise it is
    returned in the order of the dateset
    """
    def __init__(self):
        self.steps = 0
        self.shuffle = False

    def _batch(self, i):
        pass

    def __getitem__(self, i):
        return self._batch(i)

    def __iter__(self):
        shuffle = np.random.permutation(np.arange(self.steps)) if self.shuffle else np.arange(self.steps)
        for i in range(self.steps):
            si = shuffle[i]
            yield self._batch(si)

    def __len__(self):
        return self.steps


@export
class ExampleDataFeed(DataFeed):

    """Abstract base class that works on a list of examples

    """
    def __init__(self, examples, batchsz, shuffle=True, trim=False, truncate=False):
        """Constructor from a list of examples

        Use the examples requested to provide data.  Options for batching and shuffling are supported,
        along with some optional processing function pointers

        :param examples: A list of examples
        :param batchsz: Batch size per step
        :param kwargs: See below

        :Keyword Arguments:
            * *shuffle* -- Shuffle the data per epoch? Defaults to `False`
            * *vec_alloc* -- Allocate a new tensor.  Defaults to ``numpy.zeros``
            * *vec_shape* -- Function to retrieve tensor shape.  Defaults to ``numpy.shape``
            * *trim* -- Trim batches to the maximum length seen in the batch (defaults to `False`)
                This can lead to batches being shorter than the maximum length provided to the system.
                Not supported in all frameworks.
            * *src_vec_trans* -- A transform function to use on the source tensor (`None`)
            * *truncate* -- bool, If true the datastream will be cut short when
                a full batch cannot be made, otherwise the final batch is smaller
                than normal batches.
        """
        super().__init__()

        self.examples = examples
        self.batchsz = batchsz
        self.shuffle = shuffle
        self.truncate = truncate
        if self.truncate:
            self.steps = int(math.floor(len(self.examples) / float(batchsz)))
        else:
            self.steps = (len(self.examples) + batchsz - 1) // batchsz
        self.trim = trim

    def _batch(self, i):
        """
        Get a batch of data at step `i`
        :param i: (``int``) step index
        :return: A batch tensor x, batch tensor y
        """
        batch = self.examples.batch(i, self.batchsz, trim=self.trim)
        return batch

try:
    from torch.utils.data import DataLoader
    from torch.utils.data.distributed import DistributedSampler
    from baseline.pytorch.torchy import DatasetAdapter
except:
    pass
try:
    from baseline.tf.tfy import TFDataAdapter, TFSeqDataAdapter
except:
    pass


def data_feed(examples, batch_size=1, shuffle=True,
              is_distributed=False, num_workers=0,
              prefetch=2, trim=False, truncate=False, pin_memory=True, backend=None):

    use_native = str2bool(os.getenv('MEAD_NATIVE_LOADER', True))
    if use_native:
        if backend == 'pytorch':
            if trim:
                logger.warning("Warning, trim is not currently supported with PyTorch native loader")
            dataset = DatasetAdapter(examples)
            do_shuffle = shuffle
            sampler = None
            if shuffle and is_distributed:
                do_shuffle = False
                sampler = DistributedSampler(dataset)
            loader = DataLoader(dataset, batch_size, shuffle=do_shuffle, sampler=sampler, prefetch_factor=prefetch,
                                num_workers=num_workers, pin_memory=pin_memory, drop_last=truncate)
            return loader

        elif backend == 'tf':
            if trim:
                logger.warning("Warning, trim is not currently supported with TF native loader")
            return TFDataAdapter(examples, batch_size, shuffle, prefetch=prefetch, num_workers=num_workers,
                                 is_distributed=is_distributed, truncate=truncate)

    if is_distributed:
        raise Exception("Example data feed is not distributable")
    return ExampleDataFeed(examples, batch_size, shuffle, trim, truncate)


@export
class DictExamples:
    """This object holds a list of dictionaries, and knows how to shuffle, sort and batch them
    """
    def __init__(self, example_list, do_shuffle=True, sort_key=None):
        """Constructor

        :param example_list:  A list of examples
        :param do_shuffle: (``bool``) Shuffle the data? Defaults to `True`
        :param do_sort: (``bool``) Sort the data.  Defaults to `True`
        """
        self.example_list = example_list

        if do_shuffle:
            random.shuffle(self.example_list)

        if sort_key is not None:
            self.example_list = sorted(self.example_list, key=lambda x: x[sort_key])

        self.sort_key = sort_key

    def __getitem__(self, i):
        """Get a single example

        :param i: (``int``) simple index
        :return: an example
        """
        return self.example_list[i]

    def __len__(self):
        """Number of examples

        :return: (``int``) length of data
        """
        return len(self.example_list)

    def _trim_batch(self, batch, keys, max_src_len):
        if max_src_len == 0:
            return batch
        for k in keys:
            if len(batch[k].shape) == 3:
                batch[k] = batch[k][:, 0:max_src_len, :]
            elif len(batch[k].shape) == 2:
                batch[k] = batch[k][:, :max_src_len]
        return batch

    def batch(self, start, batchsz, trim=False):

        """Get a batch of data

        :param start: (``int``) The step index
        :param batchsz: (``int``) The batch size
        :param trim: (``bool``) Trim to maximum length in a batch
        :return batched dictionary
        """
        ex = self.example_list[start]
        keys = ex.keys()
        batch = {}

        for k in keys:
            batch[k] = []
        sz = len(self.example_list)
        idx = start * batchsz
        max_src_len = 0

        for i in range(batchsz):
            if idx >= sz:
                break

            ex = self.example_list[idx]
            for k in keys:
                batch[k].append(ex[k])

            # Trim all batches along the sort_key if it exists
            if trim and self.sort_key is not None:
                max_src_len = max(max_src_len, ex[self.sort_key])
            idx += 1

        for k in keys:
            batch[k] = np.stack(batch[k])
        return self._trim_batch(batch, keys, max_src_len) if trim else batch


@export
class Seq2SeqExamples:

    """Paired training examples
    """
    def __init__(self, example_list, do_shuffle=True, src_sort_key=None):
        """Constructor

        :param example_list: Training pair examples
        :param do_shuffle: Shuffle the data (defaults to `True`)
        :param do_sort: Sort the data (defaults to `True`)
        """
        self.example_list = example_list
        if do_shuffle:
            random.shuffle(self.example_list)
        if src_sort_key is not None:
            self.example_list = sorted(self.example_list, key=lambda x: x[src_sort_key])
        self.src_sort_key = src_sort_key

    def __getitem__(self, i):
        """Get `ith` example

        :param i: (``int``) index of example
        :return: example dict
        """
        return self.example_list[i]

    def __len__(self):
        return len(self.example_list)

    def _trim_batch(self, batch, max_src_len, max_tgt_len):
        for k in batch.keys():
            max_len = max_src_len
            if k == 'tgt':
                max_len = max_tgt_len

            if max_len == 0:
                continue
            if len(batch[k].shape) == 3:
                batch[k] = batch[k][:, 0:max_len, :]
            elif len(batch[k].shape) == 2:
                batch[k] = batch[k][:, :max_len]

        return batch

    def batch(self, start, batchsz, trim=False):

        """Get a batch of data

        :param start: (``int``) The step index
        :param batchsz: (``int``) The batch size
        :param trim: (``bool``) Trim to maximum length in a batch
        :param vec_alloc: A vector allocator
        :param vec_shape: A vector shape function
        :return: batched `x` word vector, `x` character vector, batched `y` vector, `length` vector, `ids`
        """
        ex = self.example_list[start]
        keys = ex.keys()
        batch = {}

        for k in keys:
            batch[k] = []
        sz = len(self.example_list)
        idx = start * batchsz
        max_src_len = 0
        max_tgt_len = 0
        for i in range(batchsz):
            if idx >= sz:
                break

            ex = self.example_list[idx]
            for k in keys:
                batch[k].append(ex[k])

            # Trim all batches along the sort_key if it exists
            if trim and self.src_sort_key is not None:
                max_src_len = max(max_src_len, ex[self.src_sort_key])

            if trim:
                max_tgt_len = max(max_tgt_len, ex['tgt_lengths'])

            idx += 1

        for k in keys:
            batch[k] = np.stack(batch[k])
        return self._trim_batch(batch, max_src_len, max_tgt_len) if trim else batch


# This one is a little different at the moment
@export
class SeqWordCharDataFeed(DataFeed):
    """Data feed to return language modeling training data
    """

    def __init__(self, examples, nctx, batchsz, tgt_key=None):
        """Constructor
        :param examples: word tensor
        :param nctx: Number of steps of BPTT
        :param batchsz: Batch size
        :param tgt_key: Which field to treat as the target key (this will share an embedding vocab with the source)
        """
        super().__init__()
        self.examples = examples
        # This identifies which vector to use for targets
        self.tgt_key = 'x' if tgt_key is None else tgt_key
        num_examples = examples['{}_dims'.format(tgt_key)][0]
        rest = num_examples // batchsz
        self.steps = rest // nctx
        self.nctx = nctx
        self.batchsz = batchsz

    def keys(self):
        return list(self.examples.keys()) + ['y']

    def _batch(self, i):

        example = {}
        for k in self.examples.keys():
            if k.endswith('_dims'):
                continue
            # TODO: is this inefficient?
            x = self.examples[k].reshape(self.batchsz, -1)
            dims = self.examples['{}_dims'.format(k)]
            if len(dims) == 1:
                width = 1
            else:
                width = dims[1]

            example[k] = x[:, i*self.nctx * width:(i + 1) * self.nctx * width]

            if len(dims) == 1:
                reshape_dims = (self.batchsz, self.nctx)
            else:
                reshape_dims = (self.batchsz, self.nctx, width)
            example[k] = example[k].reshape(reshape_dims)
            if self.tgt_key == k:
                example['y'] = x[:, i*self.nctx * width + 1:(i + 1) * self.nctx * width + 1].reshape(reshape_dims)

        return example


def lm_data_feed(examples, nctx, batch_size, tgt_key=None,
                 is_distributed=False,
                 prefetch=2, backend=None):

    use_native = str2bool(os.getenv('MEAD_NATIVE_LOADER', True))
    dataset = SeqWordCharDataFeed(examples, nctx, batch_size, tgt_key=tgt_key)
    if use_native and backend == 'tf':
        print('Using Native loader', backend)
        return TFSeqDataAdapter(dataset, prefetch=prefetch,
                                is_distributed=is_distributed)

    if is_distributed:
        raise Exception("SeqWordCharDataFeed is not distributable")

    return dataset

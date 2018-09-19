import baseline.data
import numpy as np
from collections import Counter
import re
import codecs
from baseline.utils import import_user_module, revlut, export
from baseline.vectorizers import Dict1DVectorizer, GOVectorizer
import os

__all__ = []
exporter = export(__all__)


@exporter
def num_lines(filename):
    lines = 0
    with codecs.open(filename, encoding='utf-8', mode='r') as f:
        for _ in f:
            lines = lines + 1
    return lines


def _build_vocab_for_col(col, files, vectorizers):
    vocabs = dict()

    for key in vectorizers.keys():
        vocabs[key] = Counter()
        vocabs[key]['<UNK>'] = 100000  # In case freq cutoffs
        vocabs[key]['<EOS>'] = 100000  # In case freq cutoffs
    for file in files:
        if file is None:
            continue
        with codecs.open(file, encoding='utf-8', mode='r') as f:
            for line in f:
                cols = re.split("\t", line)
                text = re.split("\s", cols[col])
                for key, vectorizer in vectorizers.items():
                    vocabs[key].update(vectorizer.count(text))
    return vocabs


@exporter
class ParallelCorpusReader(object):

    def __init__(self,
                 src_vectorizers,
                 dst_vectorizer,
                 trim=False):

        self.src_vectorizers = src_vectorizers
        self.dst_vectorizer = GOVectorizer(dst_vectorizer)
        self.trim = trim

    def build_vocabs(self, files):
        pass

    def load_examples(self, tsfile, vocab1, vocab2):
        pass

    def load(self, tsfile, vocab1, vocab2, batchsz, shuffle=False, sort_key=None):
        examples = self.load_examples(tsfile, vocab1, vocab2)
        return baseline.data.ExampleDataFeed(examples, batchsz,
                                             shuffle=shuffle, trim=self.trim, sort_key=sort_key)


@exporter
class TSVParallelCorpusReader(ParallelCorpusReader):

    def __init__(self,
                 src_vectorizers, dst_vectorizer,
                 trim=False, src_col_num=0, dst_col_num=1):
        super(TSVParallelCorpusReader, self).__init__(src_vectorizers, dst_vectorizer, trim)
        self.src_col_num = src_col_num
        self.dst_col_num = dst_col_num

    def build_vocabs(self, files):
        src_vocab = _build_vocab_for_col(self.src_col_num, files, self.src_vectorizers)
        dst_vocab = _build_vocab_for_col(self.dst_col_num, files, {'dst': self.dst_vectorizer})
        return src_vocab, dst_vocab['dst']

    def load_examples(self, tsfile, src_vocabs, dst_vocab):
        ts = []
        with codecs.open(tsfile, encoding='utf-8', mode='r') as f:
            for line in f:
                splits = re.split("\t", line.strip())
                src = list(filter(lambda x: len(x) != 0, re.split("\s+", splits[0])))

                example = {}
                for k, vectorizer in self.src_vectorizers.items():
                    example[k], length = vectorizer.run(src, src_vocabs[k])
                    if length is not None:
                        example['{}_lengths'.format(k)] = length

                dst = list(filter(lambda x: len(x) != 0, re.split("\s+", splits[1])))
                example['dst'], example['dst_lengths'] = self.dst_vectorizer.run(dst, dst_vocab)
                ts += [example]
        return baseline.data.Seq2SeqExamples(ts)


@exporter
class MultiFileParallelCorpusReader(ParallelCorpusReader):

    def __init__(self, src_suffix, dst_suffix, src_vectorizers, dst_vectorizer, trim=False):
        super(MultiFileParallelCorpusReader, self).__init__(src_vectorizers, dst_vectorizer, trim)
        self.src_suffix = src_suffix
        self.dst_suffix = dst_suffix
        if not src_suffix.startswith('.'):
            self.src_suffix = '.' + self.src_suffix
        if not dst_suffix.startswith('.'):
            self.dst_suffix = '.' + self.dst_suffix

    # 2 possibilities here, either we have a vocab file, e.g. vocab.bpe.32000, or we are going to generate
    # from each column
    def build_vocabs(self, files):
        if len(files) == 1 and os.path.exists(files[0]):
            src_vocab = _build_vocab_for_col(0, files, self.src_vectorizers)
            dst_vocab = _build_vocab_for_col(0, files, {'dst': self.dst_vectorizer})
        else:
            src_vocab = _build_vocab_for_col(0, [f + self.src_suffix for f in files], self.src_vectorizers)
            dst_vocab = _build_vocab_for_col(0, [f + self.dst_suffix for f in files], {'dst': self.dst_vectorizer})
        return src_vocab, dst_vocab['dst']

    def load_examples(self, tsfile, src_vocabs, dst_vocab):
        ts = []

        with codecs.open(tsfile + self.src_suffix, encoding='utf-8', mode='r') as fsrc:
            with codecs.open(tsfile + self.dst_suffix, encoding='utf-8', mode='r') as fdst:
                for src, dst in zip(fsrc, fdst):
                    example = {}
                    src = re.split("\s+", src.strip())
                    for k, vectorizer in self.src_vectorizers.items():
                        example[k], length = vectorizer.run(src, src_vocabs[k])
                        if length is not None:
                            example['{}_lengths'.format(k)] = length
                    dst = re.split("\s+", dst.strip())
                    example['dst'], example['dst_lengths'] = self.dst_vectorizer.run(dst, dst_vocab)
                    ts += [example]
        return baseline.data.Seq2SeqExamples(ts)


@exporter
def create_parallel_corpus_reader(src_vectorizers, dst_vectorizer, trim, **kwargs):

    reader_type = kwargs.get('reader_type', 'default')

    if reader_type == 'default':
        print('Reading parallel file corpus')
        pair_suffix = kwargs.get('pair_suffix')
        reader = MultiFileParallelCorpusReader(pair_suffix[0], pair_suffix[1], src_vectorizers, dst_vectorizer, trim)
    elif reader_type == 'tsv':
        print('Reading tab-separated corpus')
        reader = TSVParallelCorpusReader(src_vectorizers, dst_vectorizer, trim)
    else:
        mod = import_user_module("reader", reader_type)
        return mod.create_parallel_corpus_reader(src_vectorizers, dst_vectorizer, trim, **kwargs)
    return reader


@exporter
class SeqPredictReader(object):

    def __init__(self,
                 vectorizers,
                 trim=False):
        self.vectorizers = vectorizers
        self.trim = trim
        self.label2index = {"<PAD>": 0, "<GO>": 1, "<EOS>": 2}
        self.label_vectorizer = Dict1DVectorizer(fields='y', mxlen=-1)

    def build_vocab(self, files):

        vocabs = {}
        for key in self.vectorizers.keys():
            vocabs[key] = Counter()
            vocabs[key]['<UNK>'] = 100000  # In case freq cutoffs

        labels = Counter()
        for file in files:
            if file is None:
                continue

            examples = self.read_examples(file)
            for example in examples:
                labels.update(self.label_vectorizer.count(example))
                for k, vectorizer in self.vectorizers.items():
                    vocab_example = vectorizer.count(example)
                    vocabs[k].update(vocab_example)

        base_offset = len(self.label2index)
        for i, k in enumerate(labels.keys()):
            self.label2index[k] = i + base_offset
        return vocabs

    def read_examples(self):
        pass

    def load(self, filename, vocabs, batchsz, shuffle=False, sort_key=None):

        ts = []
        texts = self.read_examples(filename)

        if sort_key is not None and not sort_key.endswith('_lengths'):
            sort_key += '_lengths'

        for i, example_tokens in enumerate(texts):
            example = {}
            for k, vectorizer in self.vectorizers.items():
                example[k], lengths = vectorizer.run(example_tokens, vocabs[k])
                if lengths is not None:
                    example['{}_lengths'.format(k)] = lengths
            example['y'], lengths = self.label_vectorizer.run(example_tokens, self.label2index)
            example['y_lengths'] = lengths
            example['ids'] = i
            ts.append(example)
        examples = baseline.data.DictExamples(ts, do_shuffle=shuffle, sort_key=sort_key)
        return baseline.data.ExampleDataFeed(examples, batchsz=batchsz, shuffle=shuffle, trim=self.trim), texts


@exporter
class CONLLSeqReader(SeqPredictReader):

    UNREP_EMOTICONS = (
        ':)',
        ':(((',
        ':D',
        '=)',
        ':-)',
        '=(',
        '(=',
        '=[[',
    )

    def __init__(self, vectorizers, trim=False, **kwargs):
        super(CONLLSeqReader, self).__init__(vectorizers, trim)
        self.named_fields = kwargs.get('named_fields', {})


    @staticmethod
    def web_cleanup(word):
        if word.startswith('http'): return 'URL'
        if word.startswith('@'): return '@@@@'
        if word.startswith('#'): return '####'
        if word == '"': return ','
        if word in CONLLSeqReader.UNREP_EMOTICONS: return ';)'
        if word == '<3': return '&lt;3'
        return word

    def read_examples(self, tsfile):

        tokens = []
        examples = []

        with codecs.open(tsfile, encoding='utf-8', mode='r') as f:
            for line in f:
                states = re.split("\s", line.strip())

                token = dict()
                if len(states) > 1:
                    for j in range(len(states)):
                        noff = j - len(states)
                        if noff >= 0:
                            noff = j
                        field_name = self.named_fields.get(str(j),
                                                           self.named_fields.get(str(noff), str(j)))
                        token[field_name] = states[j]
                    tokens += [token]

                else:
                    examples += [tokens]
                    tokens = []

        return examples


@exporter
def create_seq_pred_reader(vectorizers, trim, **kwargs):

    reader_type = kwargs.get('reader_type', 'default')

    if reader_type == 'default':
        print('Reading CONLL sequence file corpus')
        reader = CONLLSeqReader(vectorizers, trim, **kwargs)
    else:
        mod = import_user_module("reader", reader_type)
        reader = mod.create_seq_pred_reader(vectorizers, trim, **kwargs)
    return reader


@exporter
class SeqLabelReader(object):

    def __init__(self):
        pass

    def build_vocab(self, files, **kwargs):
        pass

    def load(self, filename, index, batchsz, **kwargs):
        pass


@exporter
class TSVSeqLabelReader(SeqLabelReader):

    REPLACE = { "'s": " 's ",
                "'ve": " 've ",
                "n't": " n't ",
                "'re": " 're ",
                "'d": " 'd ",
                "'ll": " 'll ",
                ",": " , ",
                "!": " ! ",
                }

    def __init__(
            self,
            vectorizers, clean_fn=None, trim=False
    ):
        super(TSVSeqLabelReader, self).__init__()

        self.vocab = None
        self.label2index = {}
        self.vectorizers = vectorizers
        self.clean_fn = clean_fn
        if self.clean_fn is None:
            self.clean_fn = lambda x: x
        self.trim = trim

    SPLIT_ON = '[\t\s]+'

    @staticmethod
    def splits(text):
        return list(filter(lambda s: len(s) != 0, re.split('\s+', text)))

    @staticmethod
    def do_clean(l):
        l = l.lower()
        l = re.sub(r"[^A-Za-z0-9(),!?\'\`]", " ", l)
        for k, v in TSVSeqLabelReader.REPLACE.items():
            l = l.replace(k, v)
        return l.strip()

    @staticmethod
    def label_and_sentence(line, clean_fn):
        label_text = re.split(TSVSeqLabelReader.SPLIT_ON, line)
        label = label_text[0]
        text = label_text[1:]
        text = ' '.join(list(filter(lambda s: len(s) != 0, [clean_fn(w) for w in text])))
        text = list(filter(lambda s: len(s) != 0, re.split('\s+', text)))
        return label, text

    def build_vocab(self, files, **kwargs):
        """Take a directory (as a string), or an array of files and build a vocabulary
        
        Take in a directory or an array of individual files (as a list).  If the argument is
        a string, it may be a directory, in which case, all files in the directory will be loaded
        to form a vocabulary.
        
        :param files: Either a directory (str), or an array of individual files
        :return: 
        """
        label_idx = len(self.label2index)
        if type(files) == str:
            if os.path.isdir(files):
                base = files
                files = filter(os.path.isfile, [os.path.join(base, x) for x in os.listdir(base)])
            else:
                files = [files]
        vocab = dict()
        for k in self.vectorizers.keys():
            vocab[k] = Counter()
            vocab[k]['<UNK>'] = 100000  # In case freq cutoffs

        for file in files:
            if file is None:
                continue
            with codecs.open(file, encoding='utf-8', mode='r') as f:
                for il, line in enumerate(f):
                    label, text = TSVSeqLabelReader.label_and_sentence(line, self.clean_fn)
                    if len(text) == 0:
                        continue

                    for k, vectorizer in self.vectorizers.items():
                        vocab_file = vectorizer.count(text)
                        vocab[k].update(vocab_file)

                    if label not in self.label2index:
                        self.label2index[label] = label_idx
                        label_idx += 1

        return vocab, self.get_labels()

    def get_labels(self):
        labels = [''] * len(self.label2index)
        for label, index in self.label2index.items():
            labels[index] = label
        return labels

    def load(self, filename, vocabs, batchsz, **kwargs):

        shuffle = kwargs.get('shuffle', False)
        sort_key = kwargs.get('sort_key', None)
        if sort_key is not None and not sort_key.endswith('_lengths'):
            sort_key += '_lengths'

        examples = []

        with codecs.open(filename, encoding='utf-8', mode='r') as f:
            for il, line in enumerate(f):
                label, text = TSVSeqLabelReader.label_and_sentence(line, self.clean_fn)
                if len(text) == 0:
                    continue
                y = self.label2index[label]
                example_dict = dict()
                for k, vectorizer in self.vectorizers.items():
                    example_dict[k], lengths = vectorizer.run(text, vocabs[k])
                    if lengths is not None:
                        example_dict['{}_lengths'.format(k)] = lengths

                example_dict['y'] = y
                examples.append(example_dict)
        return baseline.data.ExampleDataFeed(baseline.data.DictExamples(examples,
                                                                        do_shuffle=shuffle,
                                                                        sort_key=sort_key),
                                             batchsz=batchsz, shuffle=shuffle, trim=self.trim)


@exporter
def create_pred_reader(vectorizers, clean_fn, **kwargs):
    reader_type = kwargs.get('reader_type', 'default')

    if reader_type == 'default':
        trim = kwargs.get('trim', False)
        #splitter = kwargs.get('splitter', '[\t\s]+')
        reader = TSVSeqLabelReader(vectorizers, clean_fn=clean_fn, trim=trim)
    else:
        mod = import_user_module("reader", reader_type)
        reader = mod.create_pred_reader(vectorizers, clean_fn=clean_fn, **kwargs)
    return reader


@exporter
class LineSeqReader(object):

    def __init__(self, max_word_length, nbptt):
        self.max_word_length = max_word_length
        self.nbptt = nbptt

    def build_vocab(self, files):

        vocabs = {}
        for key in self.vectorizers.keys():
            vocabs[key] = Counter()
        for file in files:
            if file is None:
                continue

            with codecs.open(file, encoding='utf-8', mode='r') as f:

                for line in f:
                    sentence = line.split()
                    for k, vectorizer in self.vectorizers.items():
                        vectorizer.run(sentence)
                    #sentence = [w for w in sentence] + ['<EOS>']
                    #num_words += len(sentence)
                    #for w in sentence:
                    #    vocab_word[self.cleanup_fn(w)] += 1
                    #    maxw = max(maxw, len(w))
                    #    for k in w:
                    #        vocab_ch[k] += 1
                #num_words_in_files.append(num_words)

        #self.max_word_length = min(maxw, self.max_word_length) if self.max_word_length > 0 else maxw

        #print('Max word length %d' % self.max_word_length)

        return vocabs

    def load(self, filename, vocabs, batchsz):

        x = dict()

        # This isnt particularly efficient, but it allows the reader to remain agnostic of
        # its content.  We just need to know if the vectorizer has more than 1 dim
        with codecs.open(filename, encoding='utf-8', mode='r') as f:
            for line in f:
                sentence = line.split() # + ['<EOS>']
                for k, vectorizer in self.vectorizers.items():
                    vec, valid_lengths = vectorizer.run(sentence, vocabs[k])
                    x[k] = np.concatenate([x[k], vec[:valid_lengths]])

        for k, vectorizer in self.vectorizers.items():
            x['{}_dims'.format(k)] = vectorizer.get_dims()
        return baseline.data.SeqWordCharDataFeed(x, self.nbptt, batchsz)


@exporter
class LineSeqCharReader(object):

    def __init__(self, nbptt):
        self.nbptt = nbptt

    def build_vocab(self, files):
        vocab_ch = Counter()
        num_chars_in_files = []
        for file in files:
            if file is None:
                continue

            with codecs.open(file, encoding='utf-8', mode='r') as f:
                num_chars = 0
                for line in f:
                    for c in line:
                        c = self.cleanup_fn(c)
                        vocab_ch[c] += 1
                        num_chars += 1
                num_chars_in_files.append(num_chars)

        vocab = {'char': vocab_ch}
        return vocab, num_chars_in_files

    def load(self, filename, word2index, num_chars, batchsz):
        chars_vocab = word2index['char']
        x = np.zeros(num_chars, np.int)
        i = 0
        with codecs.open(filename, encoding='utf-8', mode='r') as f:
            for line in f:
                for c in line:
                    c = self.cleanup_fn(c)
                    x[i] = chars_vocab[c]
                    i += 1
        return baseline.data.SeqCharDataFeed(x, self.nbptt, batchsz)

@exporter
def create_lm_reader(max_word_length, nbptt, word_trans_fn, **kwargs):
    reader_type = kwargs.get('reader_type', 'default')

    if reader_type == 'default':
        reader = LineSeqReader(max_word_length, nbptt, word_trans_fn)
    elif reader_type == 'char_ptb':
        reader = LineSeqCharReader(nbptt, None)
    else:
        mod = import_user_module("reader", reader_type)
        reader = mod.create_lm_reader(max_word_length, nbptt, word_trans_fn, **kwargs)
    return reader

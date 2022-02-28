import logging
import time
import os
from argparse import ArgumentParser
import math
from typing import Tuple
import baseline
from eight_mile.utils import str2bool, write_json
import baseline.tf.embeddings
import baseline.embeddings
from baseline.vectorizers import BPEVectorizer1D, WordpieceVectorizer1D
from eight_mile.utils import Average, Timer, get_num_gpus_multiworker
from eight_mile.tf.layers import create_distribute_strategy, read_yaml_tf, SET_TRAIN_FLAG, set_tf_eager_debug
from eight_mile.optz import *
from eight_mile.tf.optz import *
from baseline.tf.lm import TransformerLanguageModel, TransformerMaskedLanguageModel, GatedMLPLanguageModel
from eight_mile.tf.serialize import save_tlm_npz
from collections.abc import Mapping
import tensorflow as tf
import json
logger = logging.getLogger(__file__)
# If True, this will turn of autograph compilation.  Change this flag if you want to debug
set_tf_eager_debug(str2bool(os.getenv("MEAD_TF_EAGER_DEBUG", "FALSE")))

"""Pre-train a Transformer model in TensorFlow

Read in preprocessed contexts generated by `preproc-tlm` and use a Transformer Language Model (TLM)
to train them across multiple workers.

Sample preprocessed data can be found here: https://www.dropbox.com/s/jir4layu6nzjtqy/wt2.tar.gz

"""


def loss_function(model, features, labels):
    logits, _ = model(features, None)
    loss_mask = tf.cast(labels != 0, tf.float32)
    losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=labels)
    losses = losses * loss_mask
    losses = tf.reduce_sum(losses)
    non_zero = tf.reduce_sum(loss_mask)
    losses /= non_zero
    return losses


def _parse_json(example):
    j = json.loads(example.numpy())
    return tf.constant(j['x'], dtype=tf.int32), tf.constant(j['y'], dtype=tf.int32)


feature_description = {
    'x': tf.io.FixedLenSequenceFeature([], tf.int64, allow_missing=True, default_value=0),
    'y': tf.io.FixedLenSequenceFeature([], tf.int64, allow_missing=True, default_value=0),
}


def _parse_tf_record(example_proto):
    record = tf.io.parse_single_example(example_proto, feature_description)
    return tf.cast(record['x'], dtype=tf.int32), tf.cast(record['y'], dtype=tf.int32)


def _parse_tf_record_causal(example_proto):
    record = tf.io.parse_single_example(example_proto, feature_description)
    return tf.cast(record['x'][:-1], dtype=tf.int32), tf.cast(record['x'][1:], dtype=tf.int32)


def decode_json(example):
    return tf.py_function(_parse_json, [example], [tf.int32, tf.int32])



def get_dataset(directory, file_type, num_parallel_reads=1, num_shards=1, index=0, shuffle=True, causal=False):
    """Get a dataset as a tf.data.Dataset.  Input can be a bucket or a local file


    :param directory: Either a bucket or a file
    :param file_type: Currently supports "json" files or "tfrecords"
    :param num_parallel_reads: The number of parallel reads
    :param num_shards: Number of shards to split into
    :param index: The index for this shard
    :param shuffle: Defaults to True
    :param causal: Use CLM instead of MLM (Defaults to ``False``)
    :return: a `tf.data.Dataset`
    """
    pattern = os.path.join(directory, f'*.{file_type}')
    files = tf.io.gfile.glob(pattern)
    logger.debug(files)

    if file_type == 'json':
        if causal:
            raise Exception("Causal not currently supported with JSON input")
        ds = tf.data.TextLineDataset(files, num_parallel_reads=num_parallel_reads).shard(num_shards, index)
        if shuffle:
            ds = ds.shuffle(100)
        ds = ds.map(decode_json)
        return ds
    if not shuffle:
        ds = tf.data.TFRecordDataset(files, num_parallel_reads=num_parallel_reads).shard(num_shards, index)
    else:
        ds = tf.data.Dataset.from_tensor_slices(tf.constant(files)).shard(num_shards, index)
        ds = ds.shuffle(buffer_size=len(files))
        ds = ds.interleave(lambda x: tf.data.TFRecordDataset(x),
                           num_parallel_calls=tf.data.experimental.AUTOTUNE,
                           cycle_length=num_parallel_reads,
                           deterministic=False)
        ds = ds.shuffle(buffer_size=100)
    parse_fn = _parse_tf_record_causal if causal else _parse_tf_record
    ds = ds.map(parse_fn)
    return ds


def get_num_samples(sample_md):
    yml = read_yaml_tf(sample_md)
    if not yml:
        raise Exception(f"Invalid sample file {sample_md}")
    return yml['num_samples']



def get_subword_vec1d(type):
    if type == 'bpe':
        return BPEVectorizer1D
    elif type == 'wordpiece':
        return WordpieceVectorizer1D
    else:
        from baseline.vectorizers import SentencePieceVectorizer1D
        return SentencePieceVectorizer1D


# https://github.com/OpenNMT/OpenNMT-tf/blob/master/opennmt/optimizers/utils.py
class GradientAccumulator:
    """Gradient accumulation utility.
    When used with a distribution strategy, the accumulator should be called in a
    replica context. Gradients will be accumulated locally on each replica and
    without synchronization. Users should then call ``.gradients``, scale the
    gradients if required, and pass the result to ``apply_gradients``.
    """

    # We use the ON_READ synchronization policy so that no synchronization is
    # performed on assignment. To get the value, we call .value() which returns the
    # value on the current replica without synchronization.

    def __init__(self):
        """Initializes the accumulator."""
        self._gradients = []
        self._accum_steps = None

    @property
    def step(self):
        """Number of accumulated steps."""
        if self._accum_steps is None:
            self._accum_steps = tf.Variable(
                tf.constant(0, dtype=tf.int64),
                trainable=False,
                synchronization=tf.VariableSynchronization.ON_READ,
                aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
            )
        return self._accum_steps.value()

    @property
    def gradients(self):
        """The accumulated gradients on the current replica."""
        if not self._gradients:
            raise ValueError(
                "The accumulator should be called first to initialize the gradients"
            )
        return list(gradient.value() for gradient in self._gradients)

    def __call__(self, gradients):
        """Accumulates :obj:`gradients` on the current replica."""
        if not self._gradients:
            _ = self.step  # Create the step variable.
            self._gradients.extend(
                [
                    tf.Variable(
                        tf.zeros_like(gradient),
                        trainable=False,
                        synchronization=tf.VariableSynchronization.ON_READ,
                    )
                    for gradient in gradients
                ]
            )
        if len(gradients) != len(self._gradients):
            raise ValueError(
                "Expected %s gradients, but got %d"
                % (len(self._gradients), len(gradients))
            )

        for accum_gradient, gradient in zip(self._gradients, gradients):
            accum_gradient.assign_add(gradient, read_value=False)
        self._accum_steps.assign_add(1)

    def reset(self):
        """Resets the accumulated gradients on the current replica."""
        if not self._gradients:
            return
        self._accum_steps.assign(0)
        for gradient in self._gradients:
            gradient.assign(
                tf.zeros(gradient.shape, dtype=gradient.dtype), read_value=False
            )

def main():
    parser = ArgumentParser()
    parser.add_argument("--basedir", type=str)
    parser.add_argument("--train_dir", type=str, required=True, help='Training directory')
    parser.add_argument("--valid_dir", type=str, required=True, help='Validation directory')
    parser.add_argument("--train_md", type=str, help="Training metadata YAML, defaults to `{train_dir}/md.yml`")
    parser.add_argument("--valid_md", type=str, help="Validation metadata YAML, defaults to `{valid_dir}/md.yml`")
    parser.add_argument("--dataset_key", default="tlm",
                        help="dataset key for basedir")
    parser.add_argument("--embed_type", type=str, default='default',
                        choices=["default", "positional", "learned-positional"],
                        help="register label of the embeddings")
    parser.add_argument("--d_model", type=int, default=512, help="Model dimension (and embedding dsz)")
    parser.add_argument("--d_ff", type=int, default=2048, help="FFN dimension")
    parser.add_argument("--d_k", type=int, default=None, help="Dimension per head.  Use if num_heads=1 to reduce dims")
    parser.add_argument("--num_heads", type=int, default=8, help="Number of heads")
    parser.add_argument("--num_layers", type=int, default=8, help="Number of layers")
    parser.add_argument("--num_train_workers", type=int, default=4, help="Number train workers")
    parser.add_argument("--distribute", type=str, default="mirror", choices=["mirror", "tpu", "nccl"])
    parser.add_argument("--tpu_ep", type=str, help="The TPU endpoint if using `distribute=tpu`")
    parser.add_argument("--nctx", type=int, default=512, help="Max input length")
    parser.add_argument("--file_type", default='tfrecord', choices=['json', 'tfrecord'], help="Glob pattern for data")
    parser.add_argument("--batch_size", type=int, default=256, help="Batch Size")
    parser.add_argument("--subword_model_file", type=str, help="The BPE model file", required=False)
    parser.add_argument("--subword_vocab_file", type=str, help="The BPE subword vocab", required=False)
    parser.add_argument("--subword_type", type=str, choices=["bpe", "wordpiece", "sentencepiece"], default="bpe")
    parser.add_argument("--dropout", type=float, default=0.1, help="Dropout")
    parser.add_argument("--ffn_pdrop", type=float, default=0.0, help="Dropout in the dense stack")
    parser.add_argument("--layer_drop", type=float, default=0.0, help="LayerDrop to apply")
    parser.add_argument("--optim", default="adamw", type=str, help="Optimizer to use (defaults to adamw)")
    parser.add_argument("--lr", type=float, default=4.0e-4, help="Learning rate")
    parser.add_argument("--clip", type=float, default=1.0, help="Clipping gradient norm")
    parser.add_argument("--weight_decay", type=float, default=1.0e-2, help="Weight decay")
    parser.add_argument("--epochs", type=int, default=32, help="Num training epochs")
    parser.add_argument("--restart", type=str2bool, help="Option allows you to restart from a previous checkpoint")
    parser.add_argument("--warmup_steps", type=int, default=10000, help="Num warmup steps")
    parser.add_argument("--causal", type=str2bool, default=False, help="Use CLM (causal) instead of MLM")
    parser.add_argument("--mlp", type=str2bool, default=False, help="Use Gated MLP")
    parser.add_argument("--saves_per_epoch", type=int, default=10, help="The number of checkpoints to save per epoch")
    parser.add_argument('--rpr_k',
                        help='Relative attention positional sizes pass 0 if you dont want relative attention',
                        type=int, default=[8], nargs='+')
    parser.add_argument('--rpr_value_on', type=str2bool, default=True,
                        help="In relative attention, whether add positional correction to values in addition to the "
                             "correction to attention matrix")
    parser.add_argument('--ra_type', type=str, help="Specify a relative attention type")
    parser.add_argument('--windowed_ra', type=str2bool, default=False, help="whether prevent attention beyond rpr_k")
    parser.add_argument("--strategy", help="Training strategy, defaults to `mirror`", choices=["mirror"])
    parser.add_argument("--npz", help="Should we write out NPZ files?", type=str2bool, default=False)
    parser.add_argument("--tb", help="Turn on tensorboard?", type=str2bool, default=False)
    parser.add_argument("--convert_only", help="Should we just convert this file to NPZ and exit?", type=str2bool, default=False)
    parser.add_argument("--extra_tokens", help="What extra tokens should we use", nargs="+", default=["[CLS]", "[MASK]"])
    parser.add_argument("--eps", help="Epsilon", default=1e-6, type=float)
    parser.add_argument("--beta2", help="Epsilon", default=0.98, type=float)
    parser.add_argument("--grad_accum", help="Number of iterations to accum grads", default=1, type=int)

    args = parser.parse_args()
    SET_TRAIN_FLAG(True)

    if args.convert_only:
        args.restart = True

    if args.basedir is None:
        args.basedir = f'lm-{args.dataset_key}-bpe-{os.getpid()}'
    logging.basicConfig(level=logging.INFO)
    logger.info(f"Writing results to {args.basedir}")

    if args.tb:
        logdir = f"{args.basedir}/scalars/{os.getpid()}"
        file_writer = tf.summary.create_file_writer(logdir + "/metrics")
        file_writer.set_as_default()
        logger.info(f"Set up tensorboard logdir {logdir}")

    strategy = create_distribute_strategy(args.distribute, args.tpu_ep)
    num_replicas = strategy.num_replicas_in_sync
    logger.info(f"Using {num_replicas} replicas in this job.")
    Vec1D = get_subword_vec1d(args.subword_type)
    vectorizer = Vec1D(model_file=args.subword_model_file,
                       vocab_file=args.subword_vocab_file,
                       mxlen=args.nctx,
                       extra_tokens=args.extra_tokens)

    vocab = {'x': vectorizer.vocab}
    preproc_data = baseline.embeddings.load_embeddings('x', dsz=args.d_model, known_vocab=vocab['x'],
                                                       preserve_vocab_indices=True,
                                                       embed_type=args.embed_type)
    vocabs = preproc_data['vocab']

    train_md = args.train_md if args.train_md else os.path.join(args.train_dir, 'md.yml')
    num_train_samples = get_num_samples(train_md)
    valid_md = args.valid_md if args.valid_md else os.path.join(args.valid_dir, 'md.yml')
    num_valid_samples = get_num_samples(valid_md)

    is_curriculum = True if isinstance(num_train_samples, Mapping) else False

    def dataset_train_fn(input_context):
        global_batchsz = args.batch_size
        base_batchsz = input_context.get_per_replica_batch_size(global_batchsz)
        ds = None
        num_shards = input_context.num_input_pipelines
        index = input_context.input_pipeline_id
        if is_curriculum:
            for sub in num_train_samples.keys():
                train_curr_dir = os.path.join(args.train_dir, str(sub))
                batchsz_scale_factor = args.nctx // sub
                this_batchsz = base_batchsz * batchsz_scale_factor
                curr_ds = get_dataset(train_curr_dir, args.file_type, args.num_train_workers, num_shards, index, causal=args.causal).batch(this_batchsz, drop_remainder=True)
                if ds is None:
                    ds = curr_ds
                else:
                    ds = ds.concatenate(curr_ds)
        else:
            ds = get_dataset(args.train_dir, args.file_type, args.num_train_workers, num_shards, index, causal=args.causal).batch(base_batchsz)
        return ds
    train_loader = strategy.experimental_distribute_datasets_from_function(dataset_train_fn)

    def dataset_test_fn(input_context):
        global_batchsz = args.batch_size
        base_batchsz = input_context.get_per_replica_batch_size(global_batchsz)
        num_shards = input_context.num_input_pipelines
        index = input_context.input_pipeline_id
        ds = None
        if is_curriculum:
            for sub in num_valid_samples.keys():
                valid_curr_dir = os.path.join(args.valid_dir, str(sub))
                batchsz_scale_factor = args.nctx // sub
                this_batchsz = base_batchsz * batchsz_scale_factor
                curr_ds = get_dataset(valid_curr_dir, args.file_type, args.num_train_workers, num_shards, index, causal=args.causal).batch(
                    this_batchsz, drop_remainder=True)
                if ds is None:
                    ds = curr_ds
                else:
                    ds = ds.concatenate(curr_ds)
        else:
            ds = get_dataset(args.valid_dir, args.file_type, args.num_train_workers, num_shards, index, shuffle=False, causal=args.causal).batch(base_batchsz)
        return ds
    valid_loader = strategy.experimental_distribute_datasets_from_function(dataset_test_fn)

    os.makedirs(args.basedir, exist_ok=True)
    # We want to make sure to save our input vocab into the basedir for reuse later
    write_json(vocabs, os.path.join(args.basedir, 'vocabs.json'))
    embeddings = {'x': preproc_data['embeddings']}
    logger.info("Loaded embeddings")

    logger.info("Loaded datasets")
    logger.info("Using embedding type [%s]", args.embed_type)
    model = create_model(args, embeddings)
    if isinstance(model, GatedMLPLanguageModel) and is_curriculum:
        raise Exception("Variable tensor lengths not currently supported for gMLP")
    logger.info("Loaded model and loss")

    eff_batch_size = args.batch_size * args.grad_accum
    logger.info(f"eff batch size: {eff_batch_size}, {args.batch_size}(b) x {args.grad_accum}(ga)")
    if is_curriculum:
        steps_per_epoch = 0
        steps_per_valid_epoch = 0
        for k, v in num_train_samples.items():
            steps_per_epoch += int(num_train_samples[k] // (eff_batch_size * (args.nctx / k)))
        for k, v in num_valid_samples.items():
            steps_per_valid_epoch += int(num_valid_samples[k] // (args.batch_size * (args.nctx / k)))

    else:
        steps_per_epoch = num_train_samples // eff_batch_size
        steps_per_valid_epoch = num_valid_samples // args.batch_size
    update_on = steps_per_epoch // args.saves_per_epoch
    report_on = max(10, update_on) // 10
    logger.info(f"Steps per epoch: {steps_per_epoch}. Saving checkpoint every {update_on} steps.")

    lr_decay = CosineDecaySchedulerTensorFlow(steps_per_epoch * args.epochs, lr=args.lr)
    linear_warmup = WarmupLinearSchedulerTensorFlow(args.warmup_steps, lr=args.lr)
    lr_sched = CompositeLRSchedulerTensorFlow(linear_warmup, lr_decay)
    optimizer = EagerOptimizer(loss_function, optim=args.optim, lr_function=lr_sched, weight_decay=args.weight_decay, clip=args.clip,
                               lr=args.lr, epsilon=args.eps, beta2=args.beta2)
    checkpoint = tf.train.Checkpoint(optimizer=optimizer.optimizer, model=model)
    checkpoint_manager = tf.train.CheckpointManager(checkpoint,
                                                    directory=args.basedir,
                                                    max_to_keep=5)

    grad_accum = GradientAccumulator()

    start_epoch = 0
    if args.restart:
        # The global step gets automatically updated here
        # so we dont have to worry about our LR regimen
        checkpoint.restore(checkpoint_manager.latest_checkpoint)
        current_step = optimizer.global_step
        start_epoch = current_step // steps_per_epoch

    def _replicated_forward_step(inputs):
        """This runs on a single replica"""
        x, y = inputs
        per_replica_grads, per_replica_loss = optimizer.get_grads_and_loss(model, {'x': x}, y, num_replicas * args.grad_accum)
        grad_accum(per_replica_grads)
        return per_replica_loss

    def _replicated_optz_step():
        optimizer.apply_grads(model, grad_accum.gradients)

    @tf.function
    def _distributed_optz_step():
        strategy.run(_replicated_optz_step)

    @tf.function
    def _distributed_forward_step(inputs: Tuple[tf.Tensor, tf.Tensor]):
        """Runs across multiple replicas and aggregates the results.

        :param inputs:
        :return:
        """
        per_replica_loss = strategy.run(_replicated_forward_step, args=(inputs,))
        return strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_loss, axis=None)

    def _replicated_test_step(inputs):
        """This runs on a single replica"""
        x, y = inputs
        per_replica_loss = loss_function(model, {'x': x}, y) / num_replicas
        return per_replica_loss

    @tf.function
    def _distributed_test_step(inputs: Tuple[tf.Tensor, tf.Tensor]):
        """Runs across multiple replicas and aggregates the results.

        :param inputs:
        :return:
        """
        per_replica_loss = strategy.run(_replicated_test_step, args=(inputs,))
        return strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_loss, axis=None)

    timer = Timer()
    with strategy.scope():

        for epoch in range(start_epoch, args.epochs):
            timer.start()
            SET_TRAIN_FLAG(True)
            logger.info('Starting epoch %d', epoch + 1)
            avg_loss = Average('average_train_loss')
            metrics = {}
            train_iter = iter(train_loader)

            step_loss = 0
            iterations = steps_per_epoch * args.batch_size
            for i in range(iterations):

                try:

                    loss = _distributed_forward_step(next(train_iter))
                    step_loss += loss

                    if (i + 1) % args.grad_accum == 0:
                        # This does a gradient update
                        _distributed_optz_step()
                        # Now reset the gradient accumulator
                        grad_accum.reset()
                        # Now update the loss info
                        tf.summary.scalar("train_loss", data=step_loss, step=optimizer.global_step)
                        avg_loss.update(step_loss.numpy().item())
                        # Now reset the loss
                        step_loss = 0
                        steps = optimizer.global_step.numpy()
                        if (steps + 1) % report_on == 0:
                            logger.info(avg_loss)
                        if (steps + 1) % update_on == 0:
                            elapsed = timer.elapsed(True)
                            logger.info('elapsed time this epoch %d min', elapsed)
                            logger.info('elapsed step time %f steps/min', i/elapsed)
                            checkpoint_manager.save()
                            if args.npz:
                                npz_checkpoint = os.path.join(args.basedir, f'checkpoint-step-{steps}.npz')
                                save_tlm_npz(model, npz_checkpoint)


                except Exception as e:
                    logger.error(e)
                    logger.error(f"Exception at training iter {i+1}/{iterations}. Skipping")
                    pass
                if args.convert_only:
                    logger.warning("Convert only flag specified.  Stopping after one step")
                    steps = optimizer.global_step.numpy()
                    npz_checkpoint = os.path.join(args.basedir, f'checkpoint-step-{steps}.npz')
                    save_tlm_npz(model, npz_checkpoint)
                    return



            # How much time elapsed in minutes
            train_token_loss = avg_loss.avg
            # This is the average training token-level loss across all machines
            # This is the token-level training perplexity
            train_token_ppl = math.exp(train_token_loss)
            metrics['train_elapsed_min'] = timer.elapsed(True)
            metrics['average_train_loss'] = train_token_loss
            metrics['train_ppl'] = train_token_ppl
            metrics['lr'] = float(lr_sched(tf.cast(optimizer.global_step, tf.float32)).numpy().item())

            avg_valid_loss = Average('average_valid_loss')
            timer.start()
            SET_TRAIN_FLAG(False)
            valid_iter = iter(valid_loader)
            for i in range(steps_per_valid_epoch):
                try:
                    valid_loss = _distributed_test_step(next(valid_iter))
                    tf.summary.scalar('valid_loss', data=valid_loss, step=optimizer.global_step)
                    avg_valid_loss.update(valid_loss.numpy().item())
                except Exception as e:
                    logger.error(f"Exception at validation step {i+1}/{steps_per_valid_epoch}. Skipping")
                    pass

            valid_token_loss = avg_valid_loss.avg
            valid_token_ppl = math.exp(valid_token_loss)
            metrics['valid_elapsed_min'] = timer.elapsed(True)
            metrics['average_valid_loss'] = valid_token_loss
            metrics['average_valid_word_ppl'] = valid_token_ppl
            logger.info(json.dumps(metrics, indent=4))


def create_model(args, embeddings):
    if args.mlp and args.causal:
        raise Exception("causal gMLP not implemented yet!")

    if args.mlp:
        model = GatedMLPLanguageModel.create(embeddings,
                                             nctx=args.nctx,
                                             hsz=args.d_model,
                                             d_ff=args.d_ff,
                                             tie_weights=True,
                                             dropout=args.dropout,
                                             gpu=False,
                                             layers=args.num_layers,
                                             layer_drop=args.layer_drop,
                                             ffn_drop=args.ffn_pdrop,
                                             src_keys=['x'], tgt_key='x')
    else:


        if len(args.rpr_k) == 0 or args.rpr_k[0] < 1:
            rpr_k = None
        elif len(args.rpr_k) == 1:
            rpr_k = None if args.rpr_k[0] == 0 else args.rpr_k[0]
        else:
            rpr_k = args.rpr_k

        if args.ra_type != None and args.ra_type != 'shaw' and args.rpr_k is not None:
            print(f"Relative attention mismatch. You requested {args.ra_type} with rpr set.  Setting it to 0")
            rpr_k = None
        TLM = TransformerLanguageModel if args.causal else TransformerMaskedLanguageModel
        model = TLM.create(embeddings,
                           hsz=args.d_model,
                           d_ff=args.d_ff,
                           tie_weights=True,
                           dropout=args.dropout,
                           gpu=False,
                           num_heads=args.num_heads,
                           layers=args.num_layers,
                           rpr_k=rpr_k,
                           d_k=args.d_k,
                           ffn_pdrop=args.ffn_pdrop,
                           windowed_ra=args.windowed_ra,
                           rpr_value_on=args.rpr_value_on,
                           layer_drop=args.layer_drop,
                           ra_type=args.ra_type,
                           src_keys=['x'], tgt_key='x')
    return model


if __name__ == "__main__":
    main()

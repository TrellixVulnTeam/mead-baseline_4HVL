import numpy as np
import torch
import logging
import os
import glob
from argparse import ArgumentParser
import baseline
#from eight_mile.pytorch.layers import EmbeddingsStack
from baseline.pytorch.lm import TransformerMaskedLanguageModel
from baseline.utils import str2bool, read_json, Offsets, revlut
from baseline.vectorizers import Token1DVectorizer, BPEVectorizer1D
from baseline.pytorch.embeddings import *
from transformer_utils import find_latest_checkpoint
logger = logging.getLogger(__file__)


def decode_sentence(model, vectorizer, query, word2index, index2word, device, sample=True):
    vec, length = vectorizer.run(query, word2index)
    UNK = word2index.get('<UNK>')
    MASK = word2index.get('[MASK]')
    for i in range(length):
        if vec[i] == UNK:
            vec[i] = MASK

    detok = [index2word[v] for v in vec if v != 0]
    print('[De-tok] ' + ' '.join(detok))
    toks = torch.from_numpy(vec).unsqueeze(0).to(device=device)

    length = torch.from_numpy(np.array(length)).unsqueeze(0).to(device=device)

    with torch.no_grad():
        predictions, _ = model({'x': toks}, None)
        predictions = predictions.squeeze(0)
        #predictions = model({'x': toks, 'src_len': length, 'h': None}).squeeze(0)
        words = []
        for i in range(length):

            if vec[i] == MASK:
                if not sample:
                    output = torch.argmax(predictions[i], -1).item()
                    word = index2word[output]
                else:
                    sample_dist = predictions[i].exp()
                    output = torch.multinomial(sample_dist, num_samples=1)
                    output = output.squeeze(0).item()

                    word = index2word[output]
                words.append(word)
            else:
                words.append(index2word[vec[i]])

        return words


def create_model(embeddings, d_model, d_ff, num_heads, num_layers, rpr_k, d_k, checkpoint_name):
    if len(rpr_k) == 0 or rpr_k[0] < 1:
        rpr_k = None
    logger.info("Creating tied encoder decoder model")
    model = TransformerMaskedLanguageModel.create({'x': embeddings},
                                                  hsz=d_model,
                                                  d_ff=d_ff,
                                                  tie_weights=True,
                                                  dropout=0,
                                                  gpu=False,
                                                  num_heads=num_heads,
                                                  layers=num_layers,
                                                  rpr_k=rpr_k,
                                                  d_k=d_k,
                                                  src_keys=['x'], tgt_key='x')
    model.load_state_dict(torch.load(checkpoint_name))
    model.eval()
    print(model)
    return model


def run():
    parser = ArgumentParser()
    parser.add_argument("--basedir", type=str)
    parser.add_argument("--checkpoint", type=str, help='Checkpoint name or directory to load')
    parser.add_argument("--sample", type=str2bool, help='Sample from the decoder?  Defaults to `false`', default=0)
    parser.add_argument("--vocab", type=str, help='Vocab file to load', required=False)
    parser.add_argument("--query", type=str, default='hello , <unk> are you today ?')
    parser.add_argument("--dataset_cache", type=str, default=os.path.expanduser('~/.bl-data'),
                        help="Path or url of the dataset cache")
    parser.add_argument("--d_model", type=int, default=512, help="Model dimension (and embedding dsz)")
    parser.add_argument("--d_ff", type=int, default=2048, help="FFN dimension")
    parser.add_argument("--d_k", type=int, default=None, help="Dimension per head.  Use if num_heads=1 to reduce dims")
    parser.add_argument("--num_heads", type=int, default=8, help="Number of heads")
    parser.add_argument("--num_layers", type=int, default=8, help="Number of layers")
    parser.add_argument("--nctx", type=int, default=128, help="Max context length (for both encoder and decoder)")
    parser.add_argument("--embed_type", type=str, default='positional',
                        help="register label of the embeddings, so far support positional or learned-positional")
    parser.add_argument("--subword_model_file", type=str, required=True)
    parser.add_argument("--subword_vocab_file", type=str, required=True)
    parser.add_argument('--rpr_k', help='Relative attention positional sizes pass 0 if you dont want relative attention',
                        type=int, default=[3, 5, 48, 48, 48, 48], nargs='+')

    parser.add_argument("--device", type=str,
                        default="cuda" if torch.cuda.is_available() else "cpu",
                        help="Device (cuda or cpu)")
    args = parser.parse_args()

    if torch.cuda.device_count() == 1:
        torch.cuda.set_device(0)
        args.device = torch.device("cuda", 0)


    vocab_file = args.vocab

    if os.path.isdir(args.checkpoint):
        vocab_file = os.path.join(args.checkpoint, 'vocabs.json')
        checkpoint = find_latest_checkpoint(args.checkpoint)
        logger.warning("Found latest checkpoint %s", checkpoint)
    else:
        checkpoint = args.checkpoint

    vocab = read_json(vocab_file)
    # If we are not using chars, then use 'x' for both input and output
    preproc_data = baseline.embeddings.load_embeddings('x', dsz=args.d_model, counts=False, known_vocab=vocab, embed_type=args.embed_type)
    embeddings = preproc_data['embeddings']
    vocab = preproc_data['vocab']
    model = create_model(embeddings, d_model=args.d_model, d_ff=args.d_ff, num_heads=args.num_heads, num_layers=args.num_layers,
                         rpr_k=args.rpr_k, d_k=args.d_k, checkpoint_name=checkpoint)
    model.to(args.device)

    vectorizer = BPEVectorizer1D(model_file=args.subword_model_file, vocab_file=args.subword_vocab_file, mxlen=args.nctx)
    index2word = revlut(vocab)
    print('[Query]', args.query)
    print('[Response]', ' '.join(decode_sentence(model, vectorizer, args.query.split(), vocab, index2word, args.device, sample=args.sample)))

run()

# coding: utf-8
import argparse
import time
import math
import os
import torch
import torch.nn as nn
import torch.onnx
import pickle

import sys
sys.path.append(os.path.abspath(".."))

from utils import data_train
from model import model_bidirectional

parser = argparse.ArgumentParser(description='bi-LSTM Attention based text imputation model')
parser.add_argument('--data', type=str, default=os.path.abspath(os.path.join(os.pardir,'data/penn/')),
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=0.1,
                    help='initial learning rate')   # Set for Adagrad, Change if using another optimizer
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str, default='../models/model.pt',
                    help='path to save the final model')
parser.add_argument('--onnx-export', type=str, default='',
                    help='path to export the final model in onnx format')
parser.add_argument('--threshold', type=int,
                    default=1,
                    help='Threshold for limiting vocab size of model '
                         '(anything word with frequency than this threshold will not be included)')
parser.add_argument('--dict', type=str, default='../Dictionary/dict.pt',
                    help='path to pickled dictionary')
parser.add_argument('--case', action='store_true',
                        help='use to convert all words to lowercase')
parser.add_argument('--resume', action='store_true',
                    help='resume training from an earlier checkpoint')
args = parser.parse_args()

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

device = torch.device("cuda" if args.cuda else "cpu")

###############################################################################
# Load data
###############################################################################

corpus = data_train.Corpus(args.data, args.threshold, args.case)

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.


def batchify(data, bsz, bptt):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = (((data.size(0) // bsz) - 2) // bptt) * bptt + 2
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data


eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size, args.bptt)
val_data = batchify(corpus.valid, eval_batch_size, args.bptt)
test_data = batchify(corpus.test, eval_batch_size, args.bptt)

###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)

if (args.resume == True):
    with open(args.save, 'rb') as f:
        model = torch.load(f)
        # after load the rnn params are not a continuous chunk of memory
        # this makes them a continuous chunk, and will speed up forward pass
        model.rnn_left.flatten_parameters()
        model.rnn_right.flatten_parameters()
else:
    model = model_bidirectional.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout, args.tied).to(device)

criterion = nn.CrossEntropyLoss()

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""
    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)


# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i):
    seq_len = min(args.bptt, len(source) - 1 - i)
    data_left = source[i:i+seq_len]
    target = source[i+1:i+1+seq_len].view(-1)
    data_right = source[i+2:i+2+seq_len]
    return data_left.to(device), data_right.to(device), target.to(device)


def evaluate(data_source):  #
    # Turn on evaluation mode which disables dropout.
    model.eval()
    # total_loss = 0.
    loss = 0.
    ntokens = len(corpus.dictionary)
    hidden_left = model.init_hidden(eval_batch_size)
    hidden_right = model.init_hidden(eval_batch_size)
    with torch.no_grad():
        for i in range(0, data_source.size(0) - 2, args.bptt):
            data_left, data_right, targets = get_batch(data_source, i)
            output = model(data_left, data_right, hidden_left, hidden_right)
            output_flat = output.view(-1, ntokens)
            #total_loss += len(data_left) * criterion(output_flat, targets).item() + len(data_left) * criterion(output_left.view(-1, ntokens), targets).item() + len(data_left) * criterion(output_right.view(-1, ntokens), targets).item()
            loss += len(data_left) * criterion(output_flat, targets).item()
            hidden_left = repackage_hidden(hidden_left)
            hidden_right = repackage_hidden(hidden_right)
            data_left.to("cpu")
            data_right.to("cpu")
            targets.to("cpu")
    return loss / len(data_source)


optimizer = torch.optim.Adagrad(model.parameters(), lr=args.lr, lr_decay=1e-4, weight_decay=1e-5)


def train():
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0.
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden_left = model.init_hidden(args.batch_size)
    hidden_right = model.init_hidden(args.batch_size)

    for batch, i in enumerate(range(0, train_data.size(0) - 2, args.bptt)):
        data_left, data_right, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden_left = repackage_hidden(hidden_left)
        hidden_right = repackage_hidden(hidden_right)

        optimizer.zero_grad()

        output = model(data_left, data_right, hidden_left, hidden_right)

        loss = criterion(output.view(-1, ntokens), targets) #+ criterion(output_left.view(-1, ntokens), targets) + criterion(output_right.view(-1, ntokens), targets)
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        # for p in model.parameters():
        #     p.data.add_(-lr, p.grad.data)

        optimizer.step()

        total_loss += criterion(output.view(-1, ntokens), targets).item()

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            try:
                print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                        'loss {:5.2f} | ppl {:8.2f}'.format(
                    epoch, batch, len(train_data) // args.bptt, lr,
                    elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            except OverflowError as err:
                print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                      'loss {:5.2f} | ppl INF'.format(
                    epoch, batch, len(train_data) // args.bptt, lr,
                                  elapsed * 1000 / args.log_interval, cur_loss))
            total_loss = 0
            start_time = time.time()

        data_left.to("cpu")
        data_right.to("cpu")
        targets.to("cpu")


def export_onnx(path, batch_size, seq_len):
    print('The model is also exported in ONNX format at {}'.
          format(os.path.realpath(args.onnx_export)))
    model.eval()
    dummy_input = torch.LongTensor(seq_len * batch_size).zero_().view(-1, batch_size).to(device)
    hidden = model.init_hidden(batch_size)
    torch.onnx.export(model, (dummy_input, hidden), path)


# Loop over epochs.
lr = args.lr
best_val_loss = None

# At any point you can hit Ctrl + C to break out of training early.
try:
    if args.resume == True:
        val_loss = evaluate(val_data)
        best_val_loss = val_loss
        print('| Resuming Training | valid loss {:5.2f} | valid ppl {:8.2f}'.format(val_loss, math.exp(val_loss)))

    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train()
        val_loss = evaluate(val_data)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),
                                           val_loss, math.exp(val_loss)))
        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0

except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')


with open(args.dict, "wb") as f:
    pickle.dump((corpus.dictionary, args.threshold), f)

# Load the best saved model.
with open(args.save, 'rb') as f:
    model = torch.load(f)
    # after load the rnn params are not a continuous chunk of memory
    # this makes them a continuous chunk, and will speed up forward pass
    model.rnn_left.flatten_parameters()
    model.rnn_right.flatten_parameters()

# Run on test data.
test_loss = evaluate(test_data)
print('=' * 89)
print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
    test_loss, math.exp(test_loss)))
print('=' * 89)

if len(args.onnx_export) > 0:
    # Export the model in ONNX format.
    export_onnx(args.onnx_export, batch_size=1, seq_len=args.bptt)

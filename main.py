import argparse
import logging
import tensorflow as tf
import datasets
import numpy as np
import models
import torch
import time
from torch.autograd import Variable
import os
import pickle
import monitoring
from metrics import accuracy, recall, f1_score, precision, compute_metrics_per_class, auc, record_metrics_for_epoch, summarize


def build_parser():
    parser = argparse.ArgumentParser(
        description="Model for convolution-graph network (CGN)")

    parser.add_argument('--epoch', default=10, type=int, help='The number of epochs we want ot train the network.')
    parser.add_argument('--seed', default=1993, type=int, help='Seed for random initialization and stuff.')
    parser.add_argument('--batch-size', default=100, type=int, help="The batch size.")
    parser.add_argument('--tensorboard-dir', default='./testing123/', help='The folder where to store the experiments. Will be created if not already exists.')
    parser.add_argument('--lr', default=1e-3, type=float, help='learning rate')
    parser.add_argument('--weight-decay', default=0., type=float, help='weight decay (L2 loss).')
    parser.add_argument('--l1-loss-lambda', default=0., type=float, help='L1 loss lambda.')
    parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
    parser.add_argument('--data-dir', default='/data/milatmp1/dutilfra/transcriptome/graph/', help='The folder contening the dataset.')
    parser.add_argument('--dataset', choices=['random', 'tcga-tissue', 'tcga-brca', 'tcga-label', 'tcga-gbm', 'percolate', 'nslr-syn', 'percolate-plus'], default='random', help='Which dataset to use.')
    parser.add_argument('--clinical-file', type=str, default='PANCAN_clinicalMatrix.gz', help='File to read labels from')
    parser.add_argument('--clinical-label', type=str, default='gender', help='Label to join with data')
    parser.add_argument('--scale-free', action='store_true', help='If we want a scale-free random adjacency matrix for the dataset.')
    parser.add_argument('--cuda', action='store_true', help='If we want to run on gpu.')
    parser.add_argument('--norm-adj', default=True, type=bool, help="If we want to normalize the adjancy matrix.")
    parser.add_argument('--log', choices=['tensorboard', 'console', 'silent'], default='tensorboard', help="Don't store anything in tensorboard, otherwise a segfault can happen.")
    parser.add_argument('--name', type=str, default=None, help="If we want to add a random str to the folder.")

    # Model specific options
    parser.add_argument('--num-channel', default=32, type=int, help='Number of channel in the model.')
    parser.add_argument('--dropout', action='store_true', help='If we want to perform dropout in the model..')
    parser.add_argument('--model', default='cgn', choices=['cgn', 'mlp', 'lcg', 'sgc', 'slr', 'cnn', 'random'], help='Number of channel in the CGN.')
    parser.add_argument('--num-layer', default=1, type=int, help='Number of convolution layer in the CGN.')
    parser.add_argument('--nb-class', default=None, type=int, help="Number of class for the dataset (won't work with random graph).")
    parser.add_argument('--nb-examples', default=None, type=int, help="Number of samples to train on.")
    parser.add_argument('--nb-per-class', default=None, type=int, help="Number of samples per class.")
    parser.add_argument('--train-ratio', default=0.6, type=float, help="The ratio of data to be used in the training set.")
    parser.add_argument('--percentile', default=100, type=float, help="How many edges to keep.")
    parser.add_argument('--add-self', default=True, type=bool, help="Add self references in the graph.")
    parser.add_argument('--attention-layer', default=0, type=int, help="The number of attention layer to add to the last layer. Only implemented for CGN.")
    parser.add_argument('--pool-graph', default=None, choices=['ignore', 'hierarchy'], help="If we want to pool the graph.")
    parser.add_argument('--use-emb', default=None, type=int, help="If we want to add node embeddings.")
    parser.add_argument('--use-gate', default=0., type=float, help="The lambda for the gate pooling/striding. is ignore if = 0.")
    parser.add_argument('--lambdas', default=[], type=float, nargs='*', help="A list of lambda for the specified models.")
    parser.add_argument('--size-perc', default=4, type=int, help="The size of the connected percolate graph in percolate-plus datsaet")
    parser.add_argument('--extra-cn', default=0, type=int, help="The number of extra nodes with edges in the percolate-plus dataset.")
    parser.add_argument('--extra-ucn', default=0, type=int, help="The number of extra nodes without edges in the percolate-plus dataset")
    parser.add_argument('--disconnected', default=0, type=int, help="The number of disconnected nodes from the perc subgraph without edges in the percolate-plus dataset")
    return parser

def parse_args(argv):
    if type(argv) == list or argv is None:
        opt = build_parser().parse_args(argv)
    else:
        opt = argv
    return opt


def main(argv=None):

    opt = parse_args(argv)

    # Enable us to silence logs
    logging.basicConfig(format="%(message)s")
    logger = logging.getLogger()
    if opt.log != 'silent':
        logger.setLevel('INFO')

    batch_size = opt.batch_size
    epoch = opt.epoch
    seed = opt.seed
    learning_rate = opt.lr
    weight_decay = opt.weight_decay
    momentum = opt.momentum
    on_cuda = opt.cuda
    tensorboard_dir = opt.tensorboard_dir
    nb_examples = opt.nb_examples
    nb_per_class = opt.nb_per_class
    train_ratio = opt.train_ratio
    lambdas = opt.lambdas if type(opt.lambdas) == list else [opt.lambdas]
    l1_loss_lambda = opt.l1_loss_lambda

    # The experiment unique id.
    param = vars(opt).copy()

    # Removing a bunch of useless tag
    del param['data_dir']
    del param['tensorboard_dir']
    del param['cuda']
    del param['log']
    del param['train_ratio']
    del param['epoch']
    del param['batch_size']
    del param['clinical_file']
    del param['attention_layer']
    del param['clinical_label']
    del param['nb_per_class']
    del param['lambdas']
    v_to_delete = []
    for v in param:
        if param[v] is None:
            v_to_delete.append(v)
    for v in v_to_delete:
        del param[v]

    exp_name = '_'.join(['{}={}'.format(k, v) for k, v, in param.iteritems()])

    logging.info(vars(opt))

    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.manual_seed(seed)

    # creating the dataset
    logging.info("Getting the dataset...")
    dataset = datasets.get_dataset(opt)

    # dataset loader
    train_set, valid_set, test_set = datasets.split_dataset(dataset, batch_size=batch_size, seed=seed,
                                                            nb_samples=nb_examples, train_ratio=train_ratio, nb_per_class=nb_per_class)
    nb_class = dataset.nb_class
    # Creating a model
    logging.info("Getting the model...")
    my_model = models.get_model(opt, dataset)
    logging.info("Our model:")
    logging.info(my_model)

    # Train the cgn
    criterion = torch.nn.CrossEntropyLoss(size_average=True)
    l1_criterion = torch.nn.L1Loss(size_average=False)
    optimizer = torch.optim.Adam(my_model.parameters(), lr=learning_rate, weight_decay=weight_decay)

    if on_cuda:
        logging.info("Putting the model on gpu...")
        my_model.cuda()

    writer, exp_dir = monitoring.setup_tensorboard_log(tensorboard_dir, exp_name, opt)

    max_valid = 0
    best_summary = {}

    # The training.
    for t in range(epoch):

        start_timer = time.time()

        for no_b, mini in enumerate(train_set):

            inputs, targets = mini['sample'], mini['labels']

            inputs = Variable(inputs, requires_grad=False).float()
            targets = Variable(targets, requires_grad=False).long()

            if on_cuda:
                inputs = inputs.cuda()
                targets = targets.cuda()

            # Forward pass: Compute predicted y by passing x to the model
            y_pred = my_model(inputs).float()

            # Compute and print loss
            cross_loss = criterion(y_pred, targets)
            other_loss = sum([r * l for r, l in zip(my_model.regularization(), lambdas)])
            l1_loss = models.setup_l1_loss(my_model, l1_loss_lambda, l1_criterion, on_cuda)
            total_loss = cross_loss + other_loss + l1_loss

            # Zero gradients, perform a backward pass, and update the weights.
            optimizer.zero_grad()
            total_loss.backward()
            optimizer.step()

        time_this_epoch = time.time() - start_timer
        my_model.eval()
        acc, auc = record_metrics_for_epoch(writer, cross_loss, total_loss, t, time_this_epoch, train_set, valid_set, test_set, my_model, nb_class, dataset, on_cuda)
        my_model.train()

        # small summary.
        summary= [
            t,
            cross_loss.data[0],
            total_loss.data[0],
            acc['train'],
            acc['valid'],
            acc['test'],
            auc['train'],
            auc['valid'],
            auc['test'],
            time_this_epoch
        ]
        summary = "epoch {}, cross_loss: {:.03f}, total_loss: {:.03f}, acc_train: {:0.3f}, acc_valid: {:0.3f}, acc_test:{:0.3f}, auc_train: {:0.3f}, auc_valid:{:0.3f}, auc_test:{:0.3f} time: {:.02f} sec".format(*summary)
        logging.info(summary)
        if max_valid < auc['valid'] and t != 0:
            max_valid = auc['valid']
            best_summary = summarize(t, cross_loss.data[0], total_loss.data[0], acc, auc)

    logging.info("Done!")

    if opt.log == "console":
        monitoring.monitor_everything(my_model, valid_set, opt, exp_dir)
        logging.info("Nothing will be log, everything will only be shown on screen.")
    return best_summary

if __name__ == '__main__':
    main()

from __future__ import division

import numpy as np
import os
import sys
import argparse
import torch
import torch.nn as nn
from torch import cuda

import onmt
import onmt.Models
import onmt.ModelConstructor
import onmt.modules
from onmt.Utils import aeq, use_gpu
import opts

parser = argparse.ArgumentParser(description='train.py')

# opts.py
opts.add_md_help_argument(parser)
opts.model_opts(parser)
opts.train_opts(parser)

opt = parser.parse_args()
if opt.word_vec_size != -1:
    opt.src_word_vec_size = opt.word_vec_size
    opt.tgt_word_vec_size = opt.word_vec_size

if opt.layers != -1:
    opt.enc_layers = opt.layers
    opt.dec_layers = opt.layers

opt.brnn = (opt.encoder_type == "brnn")
if opt.seed > 0:
    torch.manual_seed(opt.seed)

if opt.rnn_type == "SRU" and not opt.gpuid:
    raise AssertionError("Using SRU requires -gpuid set.")

if torch.cuda.is_available() and not opt.gpuid:
    print("WARNING: You have a CUDA device, should run with -gpuid 0")

if opt.gpuid:
    cuda.set_device(opt.gpuid[0])
    if opt.seed > 0:
        torch.cuda.manual_seed(opt.seed)

if len(opt.gpuid) > 1:
    sys.stderr.write("Sorry, multigpu isn't supported yet, coming soon!\n")
    sys.exit(1)


# Set up the Crayon logging server.
if opt.exp_host != "":
    from pycrayon import CrayonClient
    cc = CrayonClient(hostname=opt.exp_host)

    experiments = cc.get_experiment_names()
    print(experiments)
    if opt.exp in experiments:
        cc.remove_experiment(opt.exp)
    experiment = cc.create_experiment(opt.exp)


def report_func(epoch, batch, num_batches,
                start_time, lr, report_stats,
                model_ix=False):
    """
    This is the user-defined batch-level traing progress
    report function.

    Args:
        epoch(int): current epoch count.
        batch(int): current batch count.
        num_batches(int): total number of batches.
        start_time(float): last report time.
        lr(float): current learning rate.
        report_stats(Statistics): old Statistics instance.
    Returns:
        report_stats(Statistics): updated Statistics instance.
    """
    if batch % opt.report_every == -1 % opt.report_every:
        report_stats.output(epoch, batch+1, num_batches,
                            start_time, model_ix)
        if opt.exp_host:
            report_stats.log("progress", experiment, lr)
        report_stats = onmt.Statistics()

    return report_stats


def make_train_data_iter(train_data, opt):
    """
    This returns user-defined train data iterator for the trainer
    to iterate over during each train epoch. We implement simple
    ordered iterator strategy here, but more sophisticated strategy
    like curriculum learning is ok too.
    """
    return onmt.IO.OrderedIterator(
                dataset=train_data, batch_size=opt.batch_size,
                device=opt.gpuid[0] if opt.gpuid else -1,
                repeat=False)


def make_valid_data_iter(valid_data, opt):
    """
    This returns user-defined validate data iterator for the trainer
    to iterate over during each validate epoch. We implement simple
    ordered iterator strategy here, but more sophisticated strategy
    is ok too.
    """
    return onmt.IO.OrderedIterator(
                dataset=valid_data, batch_size=opt.batch_size,
                device=opt.gpuid[0] if opt.gpuid else -1,
                train=False, sort=True)


def make_loss_compute(model, tgt_vocab, dataset, opt, model_opt):
    """
    This returns user-defined LossCompute object, which is used to
    compute loss in train/validate process. You can implement your
    own *LossCompute class, by subclassing LossComputeBase.
    """
    if model_opt.ensemble and opt.copy_attn:
        compute = onmt.modules.MCLCopyGeneratorLossCompute(
            model,
            tgt_vocab,
            dataset,
            opt.copy_attn_force,
            model_opt.mcl_k,
            model_opt.ensemble_num,
            model_opt.teacher_model)
    elif model_opt.ensemble:
        compute = onmt.Loss.MCLLossCompute(model, tgt_vocab,
                                           model_opt.mcl_k,
                                           model_opt.ensemble_num,
                                           model_opt.teacher_model,
                                           model_opt.em_type)
    elif opt.copy_attn:
        compute = onmt.modules.CopyGeneratorLossCompute(
            model.generator, tgt_vocab, dataset, opt.copy_attn_force)
    else:
        compute = onmt.Loss.NMTLossCompute(model.generator, tgt_vocab)

    if use_gpu(opt):
        compute.cuda()

    return compute


def train_model(model, train_data, valid_data, fields, optim, model_opt):

    train_iter = make_train_data_iter(train_data, opt)
    valid_iter = make_valid_data_iter(valid_data, opt)

    train_loss = make_loss_compute(model, fields["tgt"].vocab,
                                   train_data, opt, model_opt)
    valid_loss = make_loss_compute(model, fields["tgt"].vocab,
                                   valid_data, opt, model_opt)

    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches

    trainer = onmt.Trainer(model, train_iter, valid_iter,
                           train_loss, valid_loss, optim,
                           trunc_size, shard_size,
                           model_opt.ensemble,
                           model_opt.ensemble_num,
                           model_opt.pretrain_for)

    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print('')

        # 1. Train for one epoch on the training set.
        train_stats = trainer.train(epoch, report_func)
        if model_opt.ensemble:
            print(trainer.total_counts)
            for s in train_stats:
                print('Train perplexity: %g' % s.ppl())
                print('Train accuracy: %g' % s.accuracy())
        else:
            print('Train perplexity: %g' % train_stats.ppl())
            print('Train accuracy: %g' % train_stats.accuracy())

        # 2. Validate on the validation set.
        valid_stats = trainer.validate()
        if model_opt.ensemble:
            for ix, s in enumerate(valid_stats):
                print('M %d Validation perplexity: %g' % (ix+1, s.ppl()))
                print('M %d Validation accuracy: %g' % (ix+1, s.accuracy()))
        else:
            print('Validation perplexity: %g' % valid_stats.ppl())
            print('Validation accuracy: %g' % valid_stats.accuracy())

        # 3. Log to remote server.
        # if opt.exp_host:
        #     train_stats.log("train", experiment, optim.lr)
        #     valid_stats.log("valid", experiment, optim.lr)

        # 4. Update the learning rate
        if model_opt.ensemble:
            mean_ppl = np.mean([s.ppl() for s in valid_stats])
        else:
            mean_ppl = valid_stats.ppl()
        trainer.epoch_step(mean_ppl, epoch)

        # 5. Drop a checkpoint if needed.
        if epoch >= opt.start_checkpoint_at:
            trainer.drop_checkpoint(opt, epoch, fields, valid_stats)


def check_save_model_path():
    save_model_path = os.path.abspath(opt.save_model)
    model_dirname = os.path.dirname(save_model_path)
    if not os.path.exists(model_dirname):
        os.makedirs(model_dirname)


def tally_parameters(model):
    n_params = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % n_params)
    enc = 0
    dec = 0
    for name, param in model.named_parameters():
        if 'encoder' in name:
            enc += param.nelement()
        elif 'decoder' or 'generator' in name:
            dec += param.nelement()
    print('encoder: ', enc)
    print('decoder: ', dec)


def load_fields(train, valid, checkpoint):
    fields = onmt.IO.ONMTDataset.load_fields(
                torch.load(opt.data + '.vocab.pt'))
    fields = dict([(k, f) for (k, f) in fields.items()
                  if k in train.examples[0].__dict__])
    train.fields = fields
    valid.fields = fields

    if opt.train_from:
        print('Loading vocab from checkpoint at %s.' % opt.train_from)
        fields = onmt.IO.ONMTDataset.load_fields(checkpoint['vocab'])

    print(' * vocabulary size. source = %d; target = %d' %
          (len(fields['src'].vocab), len(fields['tgt'].vocab)))

    return fields


def collect_features(train, fields):
    # TODO: account for target features.
    # Also, why does fields need to have the structure it does?
    src_features = onmt.IO.ONMTDataset.collect_features(fields)
    aeq(len(src_features), train.nfeatures)

    return src_features


def build_model(model_opt, opt, fields, checkpoint):
    if model_opt.ensemble:
        print("Building Ensemble...")
        models = []
        for i in range(model_opt.ensemble_num):
            print("Building Model {}".format(i + 1))
            models.append(onmt.ModelConstructor.make_base_model(model_opt,
                                                                fields,
                                                                use_gpu(opt),
                                                                checkpoint))
            if model_opt.ensemble_share == "embedding" and i > 0:
                models[i].encoder.embeddings.word_lut.weight = models[0].encoder.embeddings.word_lut.weight
            elif model_opt.ensemble_share == "encoder" and i > 0:
                models[i].encoder = models[0].encoder
            elif model_opt.ensemble_share == "decoder" and i > 0:
                models[i].encoder = models[0].encoder
                models[i].decoder = models[0].decoder
        model = onmt.Models.Ensemble(models)

        if use_gpu(opt):
            model.cuda()
        else:
            model.cpu()

    else:
        print('Building model...')
        model = onmt.ModelConstructor.make_base_model(model_opt, fields,
                                                      use_gpu(opt), checkpoint)
        if len(opt.gpuid) > 1:
            print('Multi gpu training: ', opt.gpuid)
            model = nn.DataParallel(model, device_ids=opt.gpuid, dim=1)

    return model


def build_optim(model, checkpoint):
    if opt.train_from:
        print('Loading optimizer from checkpoint.')
        optim = checkpoint['optim']
        optim.optimizer.load_state_dict(
            checkpoint['optim'].optimizer.state_dict())
    else:
        # what members of opt does Optim need?
        optim = onmt.Optim(
            opt.optim, opt.learning_rate, opt.max_grad_norm,
            lr_decay=opt.learning_rate_decay,
            start_decay_at=opt.start_decay_at,
            opt=opt
        )

    optim.set_parameters(model.parameters())

    return optim


def main():

    # Load train and validate data.
    print("Loading train and validate data from '%s'" % opt.data)
    train = torch.load(opt.data + '.train.pt')
    valid = torch.load(opt.data + '.valid.pt')
    print(' * number of training sentences: %d' % len(train))
    print(' * maximum batch size: %d' % opt.batch_size)

    # Load checkpoint if we resume from a previous training.
    if opt.train_from:
        print('Loading checkpoint from %s' % opt.train_from)
        checkpoint = torch.load(opt.train_from,
                                map_location=lambda storage, loc: storage)
        model_opt = checkpoint['opt']
        # I don't like reassigning attributes of opt: it's not clear
        opt.start_epoch = checkpoint['epoch'] + 1
    else:
        checkpoint = None
        model_opt = opt

    # Load fields generated from preprocess phase.
    fields = load_fields(train, valid, checkpoint)

    # Collect features.
    src_features = collect_features(train, fields)
    for j, feat in enumerate(src_features):
        print(' * src feature %d size = %d' % (j, len(fields[feat].vocab)))

    # Build model.
    model = build_model(model_opt, opt, fields, checkpoint)
    print(model)
    tally_parameters(model)
    check_save_model_path()

    # Build optimizer.
    optim = build_optim(model, checkpoint)

    # Do training.
    train_model(model, train,
                valid, fields,
                optim, model_opt=model_opt)


if __name__ == "__main__":
    main()

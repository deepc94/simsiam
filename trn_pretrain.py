import os
import glob
import time
import data
import torch
import random
import importlib
import torch.nn as nn
from torch.nn import init
from losses import simclr
from losses import simsiam
from utils import net_utils
from utils import csv_utils
from utils import gpu_utils
from utils import path_utils
from datetime import timedelta
import torch.distributed as dist
from utils.schedulers import get_policy
from torch.utils.tensorboard import SummaryWriter
from utils.logging import AverageMeter, ProgressMeter





def trn(cfg,model):



    cfg.logger.info(cfg)
    if cfg.seed is not None:
        random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)
        torch.cuda.manual_seed(cfg.seed)
        torch.cuda.manual_seed_all(cfg.seed)

    train, validate_knn = get_trainer(cfg)

    if cfg.gpu is not None:
        cfg.logger.info("Use GPU: {} for training".format(cfg.gpu))

    # if cfg.pretrained:
    #     net_utils.load_pretrained(cfg.pretrained,cfg.multigpu[0], model)

    optimizer = get_optimizer(cfg, model)
    cfg.logger.info(f"=> Getting {cfg.set} dataset")

    dataset = getattr(data, cfg.set)(cfg)

    lr_policy = get_policy(cfg.lr_policy)(optimizer, cfg)

    if cfg.arch == 'SimSiam':
        # L = D(p1, z2) / 2 + D(p2, z1) / 2
        base_criterion = lambda bb1_z1_p1_emb, bb2_z2_p2_emb: simsiam.SimSaimLoss(bb1_z1_p1_emb[2], bb2_z2_p2_emb[1]) / 2 +\
                                                 simsiam.SimSaimLoss(bb2_z2_p2_emb[2], bb1_z1_p1_emb[1]) / 2
    elif cfg.arch == 'SimCLR':
        base_criterion = lambda z1,z2 : simclr.NT_XentLoss(z1, z2)
    else:
        raise NotImplemented

    run_base_dir, ckpt_base_dir, log_base_dir = path_utils.get_directories(cfg,cfg.gpu)
    _, zero_gpu_ckpt_base_dir, _ = path_utils.get_directories(cfg, 0)
    # if cfg.resume:
    saved_epochs = sorted(glob.glob(str(zero_gpu_ckpt_base_dir) + '/epoch_*.state'), key=os.path.getmtime)
    # assert len(epochs) < 2, 'Should be only one saved epoch -- the last one'
    if len(saved_epochs) > 0:
        cfg.resume = saved_epochs[-1]
        resume(cfg, model, optimizer)


    cfg.ckpt_base_dir = ckpt_base_dir

    writer = SummaryWriter(log_dir=log_base_dir)
    epoch_time = AverageMeter("epoch_time", ":.4f", write_avg=False)
    validation_time = AverageMeter("validation_time", ":.4f", write_avg=False)
    train_time = AverageMeter("train_time", ":.4f", write_avg=False)
    progress_overall = ProgressMeter(
        1, [epoch_time, validation_time, train_time], cfg, prefix="Overall Timing"
    )

    end_epoch = time.time()
    cfg.start_epoch = cfg.start_epoch or 0

    start_time = time.time()
    gpu_info = gpu_utils.GPU_Utils(gpu_index=cfg.gpu)

    cfg.logger.info('Start Training: Model conv 1 initialization {}'.format(torch.sum(model.module.backbone.conv1.weight)))
    # Start training

    for n,m in model.module.named_modules():
        if hasattr(m, "weight") and m.weight is not None:
            cfg.logger.info('{} ({}): {}'.format(n,type(m).__name__,m.weight.shape))


    criterion = base_criterion
    cfg.logger.info('Using Vanilla Criterion')
    for epoch in range(cfg.start_epoch, cfg.epochs):
        if cfg.world_size > 1:
            dataset.sampler.set_epoch(epoch)

        lr_policy(epoch, iteration=None)

        cur_lr = net_utils.get_lr(optimizer)

        start_train = time.time()
        train(dataset.trn_loader, model,criterion, optimizer, epoch, cfg, writer=writer)
        train_time.update((time.time() - start_train) / 60)


        if (epoch + 1) % cfg.test_interval == 0:
            if cfg.gpu == cfg.base_gpu:
                # evaluate on validation set
                start_validation = time.time()

                acc = validate_knn(dataset.trn_loader, dataset.val_loader, model.module, cfg, writer, epoch)

                validation_time.update((time.time() - start_validation) / 60)
                csv_utils.write_generic_result_to_csv(path=cfg.exp_dir,name=os.path.basename(cfg.exp_dir[:-1]),
                                                      epoch=epoch,
                                                      knn_acc=acc)

                save = (((epoch+1) % cfg.save_every) == 0) and cfg.save_every > 0
                if save or epoch == cfg.epochs - 1:
                    # if is_best:
                    # print(f"==> best {last_val_acc1:.02f} saving at {ckpt_base_dir / 'model_best.pth'}")

                    net_utils.save_checkpoint(
                        {
                            "epoch": epoch + 1,
                            "arch": cfg.arch,
                            "state_dict": model.state_dict(),
                            "ACC": acc,
                            "optimizer": optimizer.state_dict(),
                        },
                        is_best=False,
                        filename=ckpt_base_dir / f"epoch_{epoch:04d}.state",
                        save=save or epoch == cfg.epochs - 1,
                    )


                    elapsed_time = time.time() - start_time
                    seconds_todo = (cfg.epochs - epoch) * (elapsed_time / cfg.test_interval)
                    estimated_time_complete = timedelta(seconds=int(seconds_todo))
                    start_time = time.time()
                    cfg.logger.info(
                        f"==> ETA: {estimated_time_complete}\tGPU-M: {gpu_info.gpu_mem_usage()}\tGPU-U: {gpu_info.gpu_utilization()}")


                    epoch_time.update((time.time() - end_epoch) / 60)
                    progress_overall.display(epoch)
                    progress_overall.write_to_tensorboard(
                        writer, prefix="diagnostics", global_step=epoch
                    )

                    writer.add_scalar("test/lr", cur_lr, epoch)
                    end_epoch = time.time()


            if cfg.world_size > 1:
                # cfg.logger.info('GPU {} going into the barrier'.format(cfg.gpu))
                dist.barrier()
                # cfg.logger.info('GPU {} leaving the barrier'.format(cfg.gpu))


def get_trainer(args):
    args.logger.info(f"=> Using trainer from trainers.{args.trainer}")
    trainer = importlib.import_module(f"trainers.{args.trainer}")

    return trainer.train, trainer.validate


def resume(args, model, optimizer):
    if os.path.isfile(args.resume):
        args.logger.info(f"=> Loading checkpoint '{args.resume}'")

        checkpoint = torch.load(args.resume, map_location=f"cuda:{args.gpu}")
        if args.start_epoch is None:
            args.logger.info(f"=> Setting new start epoch at {checkpoint['epoch']}")
            args.start_epoch = checkpoint["epoch"]

        # best_acc1 = checkpoint["best_acc1"]

        model.load_state_dict(checkpoint["state_dict"])

        optimizer.load_state_dict(checkpoint["optimizer"])

        args.logger.info(f"=> Loaded checkpoint '{args.resume}' (epoch {checkpoint['epoch']})")

        # return best_acc1
    else:
        args.logger.info(f"=> No checkpoint found at '{args.resume}'")




def get_optimizer(args, model,fine_tune=False,criterion=None):
    for n, v in model.named_parameters():
        if v.requires_grad:
            args.logger.info("<DEBUG> gradient to {}".format(n))

        if not v.requires_grad:
            args.logger.info("<DEBUG> no gradient to {}".format(n))

    param_groups = model.parameters()
    if fine_tune:
        # Train Parameters
        param_groups = [
            {'params': list(
                set(model.parameters()).difference(set(model.model.embedding.parameters()))) if args.gpu != -1 else
            list(set(model.module.parameters()).difference(set(model.module.model.embedding.parameters())))},
            {
                'params': model.model.embedding.parameters() if args.gpu != -1 else model.module.model.embedding.parameters(),
                'lr': float(args.lr) * 1},
        ]
        if args.ml_loss == 'Proxy_Anchor':
            param_groups.append({'params': criterion.proxies, 'lr': float(args.lr) * 100})

    if args.optimizer == "sgd":
        # for name, param in model.named_parameters():
        #     print(name)
        optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                          momentum=args.momentum, weight_decay=args.weight_decay)
        # quit()
    elif args.optimizer == "adam":
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, param_groups), lr=args.lr
        )
    elif args.optimizer == 'rmsprop':
        optimizer = torch.optim.RMSprop(param_groups, lr=args.lr, alpha=0.9, weight_decay = args.weight_decay, momentum = 0.9)
    elif args.optimizer == 'adamw':
        optimizer = torch.optim.AdamW(param_groups, lr=args.lr, weight_decay = args.weight_decay)
    else:
        raise NotImplemented('Invalid Optimizer {}'.format(args.optimizer))

    return optimizer
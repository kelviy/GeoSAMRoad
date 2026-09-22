"""
Usage:
    python -m geosamroad.train --config src/geosamroad/configs/hpc/<model.yaml>
        --dataset-dir dataset/ROSADataset \
        --sam-ckpt-path checkpoints/sam_vit_b_01ec64.pth \
        --set BATCH_SIZE=2 --set TRAIN_EPOCHS=20
"""
import os
import time
from argparse import ArgumentParser
import lightning.pytorch as pl
from lightning.pytorch.callbacks import LearningRateMonitor, ModelCheckpoint
from lightning.pytorch.loggers import WandbLogger
from geosamroad.dataset.samroad_dataset import SamRoadDataModule
from geosamroad.models.model import GeoSAMRoad
from geosamroad.utils import (
    apply_overrides,
    build_dataset_config,
    finalize_config,
    load_config,
)


def parse_args():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True,
                        help="yaml config (see configs/kaggle/ and configs/template.yaml)")
    parser.add_argument("--resume", default="",
                        help="checkpoint to resume training from")
    parser.add_argument("--dataset-dir", default="",
                        help="override DATASET_DIR (ROSA root)")
    parser.add_argument("--sam-ckpt-path", default="",
                        help="override SAM_CKPT_PATH (sam_vit_b_01ec64.pth)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="override any config key (repeatable)")
    parser.add_argument("--accelerator", default="auto",
                        help="lightning accelerator (auto|gpu|cpu|mps)")
    parser.add_argument("--devices", default="auto")
    parser.add_argument("--fast-dev-run", "--fast_dev_run", action="store_true",
                        help="1 train + 1 val batch on 4 tiles, no logging/checkpoints")
    parser.add_argument("--dev-run", "--dev_run", action="store_true",
                        help="full loop on 4 tiles per split, wandb disabled")
    parser.add_argument("--final", action="store_true",
                        help="final-model run: train on train+val, log test in the val "
                             "slot for monitoring, and select checkpoints on "
                             "train_road_iou unless overwritten")
    return parser.parse_known_args()


def apply_final_run(config, overrides):
    """Fold val into train and hand the val slot to test."""
    config.FINAL_TRAIN = True
    explicit = {item.split("=", 1)[0].strip() for item in overrides or []}
    if "MONITOR" not in explicit:
        config.MONITOR = "train_road_iou"
        config.MONITOR_MODE = "max"
    print("###### FINAL RUN: train = train + val; test occupies the val slot ######")
    print(f"######   checkpoint monitor: {config.MONITOR} ({config.MONITOR_MODE}) -- "
          f"val-slot metrics (road_iou, val_loss, ...) are TEST numbers, "
          f"logged for monitoring only ######")
    if str(config.MONITOR).startswith(("val_", "road_", "keypoint_", "topo_")):
        print("###### WARNING: MONITOR reads the val slot, which now holds test -- "
              "this selects the checkpoint on test data ######")
    return config


def resolve_config(args, extra_argv=()):
    config = load_config(args.config)
    if args.dataset_dir:
        config.DATASET_DIR = args.dataset_dir
    if args.sam_ckpt_path:
        config.SAM_CKPT_PATH = args.sam_ckpt_path
    apply_overrides(config, args.overrides)
    sweep_overrides = [a[2:] for a in extra_argv
                       if a.startswith("--") and "=" in a
                       and a[2:].split("=", 1)[0].strip() in config]
    unknown = [a for a in extra_argv if f"--{a[2:]}" not in
               [f"--{o}" for o in sweep_overrides]]
    if unknown:
        raise SystemExit(
            f"unrecognised argument(s): {' '.join(unknown)}\n"
            f"config overrides are --KEY=VALUE using a key that exists in "
            f"{args.config} (e.g. --BASE_LR=1e-3)."
        )
    if sweep_overrides:
        apply_overrides(config, sweep_overrides)
        print("=== sweep parameters ===")
        for item in sweep_overrides:
            print(f"    {item}")
    if getattr(args, "final", False):
        apply_final_run(config, list(args.overrides) + list(sweep_overrides))
    return finalize_config(config)


def build_logger(config):
    in_sweep = bool(os.environ.get("WANDB_SWEEP_ID"))
    return WandbLogger(
        project=None if in_sweep else (config.get("WANDB_PROJECT") or "geosamroad"),
        entity=None if in_sweep else (config.get("WANDB_ENTITY") or None),
        name=None if in_sweep else (config.get("RUN_NAME") or None),
        mode=config.get("WANDB_MODE") or "online",
        config=config.to_dict(),
    )


class SessionTimeLimit(pl.Callback):
    """Stop the fit after `seconds` of wall time in *this* run.

    Deliberately not ``Trainer(max_time=...)``: Lightning's Timer persists its
    elapsed time into the checkpoint and restores it as an offset, so a resumed
    session would already be over budget and stop after zero epochs. A
    capped-session platform (Kaggle's ~12h) needs a fresh budget each time.

    Checked at epoch end so validation and checkpointing still run, which keeps
    last.ckpt on a clean epoch boundary for the next --resume.
    """

    def __init__(self, seconds):
        self.seconds = float(seconds)
        self.start = None

    def on_train_start(self, trainer, pl_module):
        self.start = time.monotonic()
        print(f"###### session limited to {self.seconds / 3600:.2f}h of training; "
              f"it stops at the first epoch boundary past that, resume with "
              f"--resume <checkpoint_dir>/last.ckpt ######")

    def on_train_epoch_end(self, trainer, pl_module):
        if self.start is None or trainer.should_stop:
            return
        elapsed = time.monotonic() - self.start
        if elapsed >= self.seconds:
            print(f"###### session time limit reached ({elapsed / 3600:.2f}h) at "
                  f"epoch {trainer.current_epoch}; stopping so last.ckpt is complete "
                  f"######")
            trainer.should_stop = True


def build_callbacks(config, with_logger=True):
    """ Checkpointing callback"""
    save_top_k = int(config.get("SAVE_TOP_K", 1))
    monitor = config.get("MONITOR", "road_iou")
    callbacks = [
        ModelCheckpoint(
            dirpath=config.get("CHECKPOINT_DIR") or None,
            filename=f"epoch{{epoch:02d}}-{monitor}{{{monitor}:.4f}}",
            auto_insert_metric_name=False,
            monitor=monitor,
            mode=config.get("MONITOR_MODE", "max"),
            save_top_k=save_top_k,
            save_last=save_top_k != 0,
        ),
    ]
    if with_logger:  
        callbacks += [LearningRateMonitor(logging_interval="step")]
    return callbacks


def fit(config, *, logger, accelerator="auto", devices="auto", resume="",
        dev_run=False, fast_dev_run=False, extra_callbacks=()):
    pl.seed_everything(int(config.get("SEED", 42)), workers=True)

    net = GeoSAMRoad(config)
    datamodule = SamRoadDataModule(
        build_dataset_config(config),
        batch_size=int(config.BATCH_SIZE),
        num_workers=int(config.DATA_WORKER_NUM),
        dev_run=dev_run or fast_dev_run,
    )

    callbacks = build_callbacks(config, with_logger=logger is not False)
    callbacks += list(extra_callbacks)

    single_device = str(devices).strip() in ("1", "[0]")
    strategy = ("ddp_find_unused_parameters_true" # SGCN has unused parameters
                if str(config.ENCODER_MODEL).lower() == "sgcn" and not single_device
                else "auto")

    max_hours = config.get("MAX_TRAIN_HOURS") or 0
    if max_hours:
        callbacks.append(SessionTimeLimit(float(max_hours) * 3600.0))

    trainer = pl.Trainer(
        accelerator=accelerator,
        devices=devices,
        max_epochs=int(config.TRAIN_EPOCHS),
        precision=config.get("PRECISION", 32),
        gradient_clip_val=float(config.get("GRADIENT_CLIP_VAL", 1.0)),
        accumulate_grad_batches=int(config.get("ACCUMULATE_GRAD_BATCHES", 1)),
        log_every_n_steps=int(config.get("LOG_EVERY_N_STEPS", 10)),
        check_val_every_n_epoch=1,
        num_sanity_val_steps=2,
        callbacks=callbacks,
        logger=logger,
        fast_dev_run=fast_dev_run,
        strategy=strategy,
        enable_progress_bar=bool(config.get("PROGRESS_BAR", True)),
    )
    trainer.fit(net, datamodule=datamodule, ckpt_path=resume or None)
    return trainer


def main():
    args, extra_argv = parse_args() # extra_argv: wandb --key=value pairs 
    config = resolve_config(args, extra_argv)
    dev_run = args.dev_run or args.fast_dev_run

    logger = False if dev_run else build_logger(config)

    fit(
        config,
        logger=logger,
        accelerator=args.accelerator,
        devices=args.devices,
        resume=args.resume,
        dev_run=args.dev_run,
        fast_dev_run=args.fast_dev_run,
    )


if __name__ == "__main__":
    main()

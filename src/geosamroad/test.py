"""
Usage:
    python -m geosamroad.test --config src/geosamroad/configs/hpc/<model.yaml> \
        --checkpoint checkpoints/model/best.ckpt

Sweep the ITSC/ROAD/TOPO thresholds on val rather than test:
    python -m geosamroad.test --config ... --checkpoint ... --split val
"""
from argparse import ArgumentParser

import lightning.pytorch as pl

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
                        help="yaml config the checkpoint was trained with")
    parser.add_argument("--checkpoint", default="",
                        help="trained checkpoint to evaluate (omit to test random weights)")
    parser.add_argument("--dataset-dir", default="",
                        help="override DATASET_DIR (ROSA root)")
    parser.add_argument("--sam-ckpt-path", default="",
                        help="override SAM_CKPT_PATH (sam_vit_b_01ec64.pth)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="override any config key (repeatable)")
    parser.add_argument("--accelerator", default="auto",
                        help="lightning accelerator (auto|gpu|cpu|mps)")
    parser.add_argument("--devices", default="1",
                        help="keep at 1: >1 duplicates padded samples into the metrics")
    parser.add_argument("--dev-run", "--dev_run", action="store_true",
                        help="evaluate on 4 tiles only (pipeline check)")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"],
                        help="split to run the test loop (and threshold sweep) over")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)
    if args.dataset_dir:
        config.DATASET_DIR = args.dataset_dir
    if args.sam_ckpt_path:
        config.SAM_CKPT_PATH = args.sam_ckpt_path
    apply_overrides(config, args.overrides)
    finalize_config(config)

    pl.seed_everything(int(config.get("SEED", 42)), workers=True)

    if str(args.devices) != "1":
        print(f"WARNING: --devices={args.devices}. The distributed sampler pads the "
              f"test split by repeating samples")

    net = GeoSAMRoad(config)
    datamodule = SamRoadDataModule(
        build_dataset_config(config),
        batch_size=int(config.BATCH_SIZE),
        num_workers=int(config.DATA_WORKER_NUM),
        dev_run=args.dev_run,
    )
    if args.split != "test":
        # the PR-curve threshold sweep lives in test_step, so point it at another split
        print(f"###### Running the test loop (threshold sweep) on the "
              f"{args.split} split ######")
        datamodule.test_dataloader = lambda: datamodule._dataloader(args.split)

    trainer = pl.Trainer(
        accelerator=args.accelerator,
        devices=args.devices,
        precision=config.get("PRECISION", 32),
        logger=False,
        enable_checkpointing=False,
        enable_progress_bar=bool(config.get("PROGRESS_BAR", True)),
    )
    trainer.test(net, datamodule=datamodule, ckpt_path=args.checkpoint or None)


if __name__ == "__main__":
    main()

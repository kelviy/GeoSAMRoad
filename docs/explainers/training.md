# Training

How `geosamroad.train` fits a road model on ROSA tiles, and how `geosamroad.test` and `geosamroad.triage` look at the result.

## Core idea

One YAML file per model holds every setting for a run. Command-line overrides change single keys on top of it. PyTorch Lightning then trains the model on random crops from the train split and validates on fixed crops from the val split after every epoch. It keeps the checkpoint with the best road IoU on val. A separate test script reports pixel metrics on the test split and prints the best score thresholds. A triage script draws the predicted and true road graphs side by side for the tiles that score worst after inference.

## How it works

1. **Load the config.** `load_config` reads the YAML into an attribute dict. A `_BASE_` key, if present, names a parent file that the child overrides.
2. **Apply overrides in order.** `--dataset-dir` and `--sam-ckpt-path` set their keys. Each `--set KEY=VALUE` follows. W&B sweep arguments of the form `--KEY=VALUE` come last. `--final` switches the run to final mode.
3. **Check and derive.** `finalize_config` fails when `DATASET_DIR` is empty or `ENCODER_MODEL` is not `sam`, `terramind`, `unet` or `sgcn`. It sets `PATCH_SIZE` to `CROP_SIZE * UPSCALE`, which is 256 px in every shipped config.
4. **Build the parts.** `fit` seeds everything with `SEED`, builds the `GeoSAMRoad` model and the `SamRoadDataModule`, and adds a checkpoint callback and a learning-rate monitor.
5. **Train.** A Lightning `Trainer` runs `TRAIN_EPOCHS` epochs with validation after each one. Each step adds a mask loss and a topology loss. The [models](models.md) explainer covers both losses. The [training data](training-data.md) explainer covers the crops.
6. **Test.** `geosamroad.test` loads a checkpoint and runs the test split once. It prints road IoU, F1, precision and recall at a 0.5 threshold. It then prints the threshold that maximises F1 for keypoints, roads and topology.
7. **Triage.** After [inference](inference.md) and [evaluation](evaluation.md), `geosamroad.triage` reads `results/results.csv`, sorts the tiles by one graph metric and renders the worst 20 as PNG panels.

## Details that matter

- **Shared training settings.** All seven configs in `configs/hpc/` use 100 epochs, batch size 16, 128 px crops upscaled by 2, `bce_dice` mask loss, gradient clipping at 1.0 and full 32-bit precision. They differ in encoder, input bands, `BASE_LR`, worker count and inference thresholds.
- **Optimiser.** `GeoSAMRoad.configure_optimizers` uses AdamW with betas 0.9 and 0.999 and `WEIGHT_DECAY` 0.0. With zero weight decay this is plain Adam. The pretrained encoder trains at `BASE_LR * ENCODER_LR_FACTOR`, which is 0.1 times the base rate. The decoder and TopoNet train at `BASE_LR`. SGCN has no encoder split, so it trains as one group and ignores `ENCODER_LR_FACTOR`.
- **Learning rates.** `BASE_LR` is 1e-3 for every config except `terramind.yaml`, which uses 3e-4. A `MultiStepLR` multiplies all rates by `LR_GAMMA` 0.1 once, at epoch 70.
- **Epoch size.** An epoch has `TRAIN_LEN_FACTOR` 10 random crops per train tile. Validation covers each val tile with 16 fixed crops of 128 px. The train loader has `shuffle=False`. That is harmless, because each train item draws a random tile and ignores its index.
- **Best checkpoint.** `ModelCheckpoint` monitors `MONITOR`, which is `road_iou` with mode `max` in every config. `road_iou` is the pixel IoU of the road mask on the val crops, at a fixed 0.5 threshold. The callback keeps `SAVE_TOP_K` 1 file named like `epoch42-road_iou0.5123.ckpt` and also writes `last.ckpt`. No graph metric such as APLS takes part in the choice.
- **Final mode.** `--final` sets `FINAL_TRAIN`. The train split becomes train plus val, and the val slot loads the test split. Every val metric in the logs is then a test number. The checkpoint monitor switches to `train_road_iou` unless a `--set` or sweep argument names `MONITOR`. If `MONITOR` still reads a val metric, `apply_final_run` prints a warning that the checkpoint is being picked on test data.
- **NaN steps.** `training_step` returns `None` when the loss is NaN. Lightning then skips that batch and the code prints the two loss parts.
- **Logging.** `WandbLogger` sends the whole config. Training losses and the learning rate go out every 10 steps, set by `LOG_EVERY_N_STEPS`. Train IoU and F1 go out once per epoch. Val losses go out per epoch, along with `keypoint_iou`, `road_iou`, `road_f1`, `road_precision`, `road_recall` and `topo_f1`. The first val batch of each epoch logs a table of 4 crops with RGB, true masks and predicted masks. W&B defaults to online mode, because every config sets `WANDB_MODE` to null.
- **Dev runs.** `--fast-dev-run` runs one train and one val batch on 4 tiles. `--dev-run` runs the full loop on 4 tiles per split. Both turn off W&B. `--dev-run` still writes checkpoints to `CHECKPOINT_DIR`.
- **Multi-GPU.** `--devices` defaults to `auto`. SGCN on more than one device uses `ddp_find_unused_parameters_true`, because SGCN has parameters that get no gradient. `test.py` defaults to 1 device and warns otherwise. The distributed sampler pads the test split with repeated samples, and the repeats would count twice in the metrics.
- **Override parsing.** `_parse_override` YAML-parses each value. A string that parses as a float becomes a float, so `1e-3` works even though YAML reads it as text. `null` becomes `None`. `--set` accepts any key, even one the config lacks. A sweep argument must name an existing key, or `resolve_config` exits.
- **Test thresholds.** `GeoSAMRoad.on_test_end` scans 256 binned thresholds on a precision-recall curve and prints the best F1 for each head. It writes nothing to disk. The `ITSC_THRESHOLD`, `ROAD_THRESHOLD` and `TOPO_THRESHOLD` values in the configs are all multiples of 1/255, such as 0.3216, which is 82/255. That matches this 256-bin grid.
- **Triage panels.** Each panel upsamples the tile's enhanced RGB by 4 to 2048 px. It draws the predicted graph on the left and the true graph on the right, with the score in a header bar. Tiles sort ascending, and NaN scores go last. Files land in `<run-dir>/triage/` as `<rank>_<metric>_<score>_<tile>.png`.

## Sweeps and the HPC job

`configs/sweeps/wandb.yaml` is a grid sweep over `sam.yaml` on one GPU. It tries `BASE_LR` 1e-4, 3e-4, 1e-3 and 5e-3, `ENCODER_LR_FACTOR` 0.1, 0.5 and 1.0, and `BATCH_SIZE` 8, 16 and 32. That is 36 runs of 100 epochs each. W&B ranks the runs on `road_iou`, maximised. W&B passes each value as `--KEY=VALUE`, and `resolve_config` applies those after the `--set` overrides. Inside a sweep, the logger leaves the project, entity and run name to W&B.

`scripts/hpc/train_geosamroad.sbatch` runs `sam.yaml` on one L40S GPU with 4 CPUs and a 10 hour limit. Environment variables set the paths and the main settings. Defaults are seed 42, 100 epochs, batch size 16, crop 128, upscale 2 and 3 workers. It checks that the config, the SAM weights and the dataset's `splits/` folder exist. It then calls `geosamroad.train` with `--set` for each setting. `FINAL` defaults to 1, so the default job is a final-mode run with checkpoints in `output/sam_final_geosamroad/checkpoints`.

## Why it's like this

- **Final mode.** The `--final` help text says it trains "on train+val" and logs test "in the val slot for monitoring". It switches the monitor to `train_road_iou` because the val slot now holds test. The warning in `apply_final_run` says a val-slot monitor "selects the checkpoint on test data".
- **SGCN strategy.** The code comment says SGCN has unused parameters. DDP needs `find_unused_parameters` for that case.
- **One test device.** The `--devices` help text says more than one device duplicates padded samples into the metrics.
- **Pixel IoU picks the checkpoint, not a graph metric.** Reason unknown, see KEL-37.
- **The LR drop at epoch 70, and 3e-4 for multispectral TerraMind.** Upstream SAM-Road drops at epoch 9 of 10. Reason unknown, see KEL-38.
- **Where the inference thresholds came from.** The values fit `test.py`'s 256-bin output, and `test.py` only runs on the test split. Which split produced them is not recorded. Reason unknown, see KEL-36.

## The vendored SAM-Road scripts

`src/geosamroad/sam_road/` holds the upstream SAM-Road code from the CVPRW 2024 paper. Our code imports only `sam_road.model.BilinearSampler`, `sam_road.graph_utils` and `sam_road.graph_extraction`. Nothing imports the vendored `train.py`, `test.py` or `triage.py`, and nothing reads `sam_road/config/`.

Our `train.py` started from upstream's and changes it in these ways:

- It uses one config per model with `--set` and sweep overrides. Upstream takes a single `--config`.
- It uses a `LightningDataModule` over ROSA splits. Upstream builds its own loaders with `shuffle=True`.
- It keeps the top 1 checkpoint by `road_iou`. Upstream saves every epoch with `save_top_k=-1`.
- It uses AdamW with a configurable milestone. Upstream uses Adam with a fixed drop at epoch 9.
- It removes encoder freezing and LoRA. The "Removed" comments in `configure_optimizers` mark this.
- It adds gradient clipping, seeding, NaN-step skipping, final mode and the SGCN DDP strategy.

Our `triage.visualize_image_and_graph` is upstream's with configurable node radius and edge width. `triage.main` is new.

## Weak spots and ideas

- **The checkpoint metric is not the reported metric.** Observed. The paper reports APLS, but the kept checkpoint is the one with the best val pixel IoU at a 0.5 threshold. Inference then runs at tuned road thresholds between 0.27 and 0.41. A checkpoint that is best on IoU at 0.5 may not be best on APLS at 0.3.
- **Final mode picks on training IoU.** Observed. `train_road_iou` comes from augmented training crops and tends to rise until the end. The pick is then close to "last epoch", and `last.ckpt` already holds that.
- **The default HPC job is a final run.** Observed. `FINAL=1` means a plain `sbatch` trains on val too and shows test numbers as "val". A user who expects a normal run gets a model with no clean val score.
- **Thresholds may be tuned on test.** Suspected. `test.py` only runs the test split, and the config thresholds match its output grid. If those thresholds came from test, the test APLS is optimistic. A `--split val` option would fix this.
- **The sweep ranks on the last value.** Suspected. W&B sweeps rank runs by the summary metric, which by default is the last logged `road_iou`, not the best. The sweep also runs on `sam.yaml`'s `/scratch/user/...` placeholder paths unless the machine has them. Its `program` key names `train.py`, but `command` runs `-m geosamroad.train`, so `program` is unused.
- **The README is stale.** Observed. It points at `configs/local/sam.yaml`, which does not exist. The `--config` help text points at `configs/kaggle/` and `configs/template.yaml`, which do not exist either. `test.py` and the README use `best.ckpt`, but the callback never writes a file with that name.
- **Seven near-copies of one config.** Observed. The seven files in `configs/hpc/` differ in a handful of keys. `load_config` supports `_BASE_` inheritance, but no config uses it. One base file plus small per-model files would stop the copies from drifting.
- **Config resolution exists three times.** Observed. `train.resolve_config`, `test.main` and `inferencer.py` each repeat load, path overrides, `--set` and finalize. Only `train.resolve_config` knows about sweep arguments and `--final`, and it also validates leftover argv with a hard-to-read list comprehension.
- **`--set` accepts typos.** Observed. `--set BATCHSIZE=8` adds a new key and changes nothing. Sweep arguments are checked against existing keys, so the two override paths behave differently.
- **Test results are print-only.** Observed. `test.py` runs with `logger=False` and prints the metrics and best thresholds. A user copies the thresholds into the YAML by hand. It also skips the keypoint IoU and topology F1 that validation logs.
- **Dead code.** Observed. `utils.create_output_dir_and_save_config` and `triage.rasterize_graph` have no callers. Both are upstream copies. The vendored `sam_road/train.py`, `test.py`, `triage.py`, `utils.py`, `dataset.py`, `inferencer.py` and `sam_road/config/` are never imported. They are candidates for deletion.
- **`triage.py` is a misnamed shared module.** Observed. `inferencer.py` imports `visualize_image_and_graph` from it, so a CLI script is also a drawing library. The name also clashes with the glossary, which reserves review work for the review queue. A name like `viz_worst_tiles` would be clearer. Tiles with a NaN score sort last, so tiles where a metric failed may never be shown. That last point is suspected.
- **Hard-coded values in the HPC job.** Observed. The sbatch hard-codes `sam.yaml`, an account, a personal mail address and a repo path named `InstaRoadPrototype`. Running another model means editing the script.

## Code map

- `src/geosamroad/train.py`, `resolve_config`, `apply_final_run`, `build_logger`, `build_callbacks` and `fit`: config resolution, final mode, W&B, checkpointing and the `Trainer`.
- `src/geosamroad/test.py`, `main`: test-split pixel metrics and best thresholds.
- `src/geosamroad/triage.py`, `main` and `visualize_image_and_graph`: worst-tile panels, and the graph drawing that `inferencer.py` reuses.
- `src/geosamroad/utils.py`, `load_config`, `apply_overrides`, `finalize_config` and `build_dataset_config`: config loading, overrides, checks and the dataset config.
- `src/geosamroad/models/model.py`, `GeoSAMRoad.training_step`, `validation_step`, `on_validation_epoch_end`, `on_test_end` and `configure_optimizers`: losses, logged metrics, threshold search, optimiser and schedule.
- `src/geosamroad/dataset/samroad_dataset.py`, `SamRoadDataModule`: the train, val and test loaders.
- `src/geosamroad/dataset/raster_dataset.py`, `FINAL_SPLIT_SOURCES`: which split CSVs feed each slot in final mode.
- `src/geosamroad/configs/hpc/*.yaml`: one config per encoder and band setup.
- `src/geosamroad/configs/sweeps/wandb.yaml`: the grid sweep.
- `src/geosamroad/scripts/hpc/train_geosamroad.sbatch`: the SLURM job.

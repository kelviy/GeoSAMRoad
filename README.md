## Abstract
Road extraction from medium resolution satellite imagery remains a relatively under-explored field compared to high resolution road extraction. There is growing interest in utilising Sentinel 1 & 2 imagery for downstream tasks. However, the generalisation from high-resolution to medium-resolution road extraction presents unique challenges attributed to lower resolution and domain shift. In this paper, we propose an adaptation of the SAM-Road model to extract road graphs from medium-resolution inputs. We introduce ROSA, a diverse South African dataset. Furthermore, we integrate our model into InstaGeo, an end-to-end pipeline, to support road extraction from anywhere in South Africa. 

![Road Example](images/PortElizabeth_AlbanyThicket_-33p896_25p579_Urban_r3_c2.png)

## Getting Started

### Installation

This project uses the [`uv`](https://docs.astral.sh/uv/) package manager

```bash
uv sync --all-extras
```
### Training

```bash
uv run python -m geosamroad.train --config src/geosamroad/configs/local/sam.yaml --dataset-dir /path/to/ROSADataset --sam-ckpt-path checkpoints/sam_pretrain/sam_vit_b_01ec64.pth
```

Useful flags:
- `--set KEY=VALUE` — override config key (e.g. `--set BATCH_SIZE=2 --set TRAIN_EPOCHS=20`)
- `--resume path/to/ckpt` — resume from a checkpoint

### Inference

Sliding-window inference over tiles in a split:

```bash
uv run python -m geosamroad.inferencer --config src/geosamroad/configs/local/sam.yaml --checkpoint checkpoints/sam/best.ckpt --split test --output-dir save/sam_output
```

### Evaluation

TOPO + APLS graph metrics over the predictions produced:

```bash
uv run python -m geosamroad.eval_graphs --pred-dir save/sam_output/graph --dataset-dir /path/to/ROSADataset --split test
```

## Acknowledgement
- [Segment Anything Model](https://github.com/facebookresearch/segment-anything)  
- [SAM_Road](https://github.com/htcr/sam_road) 
- [Sat2Graph](https://github.com/songtaohe/Sat2Graph)
- [SAMed](https://github.com/hitachinsk/SAMed)  
- [Detectron2](https://github.com/facebookresearch/detectron2)  
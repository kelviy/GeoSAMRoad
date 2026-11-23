# Models

How `GeoSAMRoad` in `models/model.py` turns an image crop into road and keypoint masks plus link scores, and what each of the seven configs in `configs/hpc/` swaps.

## Core idea

Every variant is SAM-Road with a different road segmentation part. A segmentation network reads a 256 px crop and predicts two masks. One mask marks road pixels and the other marks keypoints, which are intersections and dead ends. A small transformer called TopoNet then looks at pairs of candidate points and scores whether a road links them. It reads its point features from the segmentation network's feature maps. The variants change only the segmentation network and the bands it reads. TopoNet, the losses and the outputs stay the same.

## How it works

1. **Upsample the input.** The dataset reads a 128 px window of a tile with bicubic resampling, at twice the size, so the model sees 256 px at 5 m per pixel. `finalize_config` sets `PATCH_SIZE` to `CROP_SIZE * UPSCALE`, which is 128 times 2. The [training data](training-data.md) explainer covers the crops.
2. **Pick the bands.** `resolve_input` in `dataset/bands.py` chooses the bands from `ENCODER_MODEL` and `RGB_INPUT`. The dataset reads only those bands from the tile.
3. **Normalise inside the model.** The dataset passes raw values. The model multiplies by a scale, subtracts a mean and divides by a std, with stats that belong to the backbone.
4. **Segment.** `GeoSAMRoad._segment` returns mask logits of shape 2 by 256 by 256 and one or more feature maps. Channel 0 is the keypoint mask and channel 1 is the road mask.
5. **Sample point features.** `BilinearSampler` reads a feature vector at each candidate point. When the backbone gives several feature maps, `_bilinear_sample_feature_map` samples each one and concatenates the vectors.
6. **Score the links.** `TopoNet` projects each point feature to 128 numbers. For each pair it joins the source feature, the target feature and the x, y offset between them. Three transformer layers with 4 heads attend over the pairs of one source point. A linear layer gives one logit per pair.
7. **Train on both.** The loss is the mask loss plus the topology loss, added with equal weight. [Training](training.md) covers the optimiser and the schedule. [Inference](inference.md) calls `infer_masks_and_img_features` and `infer_toponet` to run the two stages separately.

## The variants

| Config | Segmentation network | Input bands | Norm stats | Point features |
| --- | --- | --- | --- | --- |
| `sam` | SAM ViT-B encoder and the SAM-Road naive decoder | 3 enhanced RGB | SAM ImageNet stats, scale 255 | 256 channels at 16 by 16 |
| `terramind_rgb` | TerraMind v1 small encoder and the same decoder | 3 enhanced RGB | TerraMind `sen2rgb` stats, scale 255 | 384 channels at 16 by 16 |
| `terramind` | TerraMind v1 small, two modalities | 10 Sentinel-2 bands and S1 VV, VH ascending | TerraMind `sen2l2a` and `sen1grd` stats, scale 10000 for S2 and 1 for S1 | 384 channels at 16 by 16 |
| `unet_rgb` | smp U-Net with an ImageNet ResNet-34 encoder | 3 enhanced RGB | ROSA train stats | 6 maps, 1008 channels |
| `unet` | the same U-Net | 12 channels | ROSA train stats | 6 maps, 1008 channels |
| `sgcn_rgb` | SGCN with a ResNet-50 layout, trained from scratch | 3 enhanced RGB | ROSA train stats | 4 maps, 960 channels |
| `sgcn` | the same SGCN | 12 channels | ROSA train stats | 4 maps, 960 channels |

The 12 channels are the 3 enhanced RGB bands, B8, B5, B6, B7, B8A, B11, B12 and S1 VV, VH ascending. They come from `ROSA_SEG_BANDS`. No variant reads elevation, slope, aspect or the urban layers.

The transformer variants, `sam` and both `terramind`, go through `_build_backbone_segmentation`. It builds a terratorch `EncoderDecoderFactory` model from the backbone wrapper, a `ReshapeTokensToImage` neck and `OriginalNaiveDecoder`. The CNN variants, `unet` and `sgcn`, replace the whole segmentation part with `UnetRoadSegmentation` or `SgcnRoadSegmentation`.

## How multi-band input reaches the model

It never reaches the SAM encoder. `resolve_input` returns three RGB bands for `sam` whatever `RGB_INPUT` says, so SAM always sees the 3-band image it was pretrained on. The extra bands reach the model only through the other backbones.

- **TerraMind** takes one tensor per modality. `GeoBackboneEncoder` slices the 12-channel stack into an `S2L2A` part of 10 bands and an `S1GRD` part of 2 bands. It names each band with its TerraMind name, such as `RED_EDGE_1`, so terratorch picks the matching pretrained patch weights. TerraMind averages the tokens of the two modalities at each patch position, because `merge_method` is `mean`.
- **U-Net** takes any channel count. smp adapts the first ImageNet convolution to 12 inputs.
- **SGCN** takes `in_channels` in its first convolution. The repo changed the upstream code for this, as the header of `SCGN_model.py` says.

## Details that matter

- **SAM at 256 px.** The SAM checkpoint is for 1024 px, which is 64 by 64 tokens. `resize_sam_pos_embed` resizes the position embedding to 16 by 16 tokens with bilinear interpolation. It also resizes the relative position tables of the 4 global attention blocks. The code loads the weights with `strict=False` and only prints the counts of missing and unexpected keys.
- **`PRETRAINED` must have a checkpoint for SAM.** When `PRETRAINED` is true and `SAM_CKPT_PATH` is empty, the model raises an error. Inference sets `PRETRAINED` to false when it loads a trained checkpoint.
- **The decoder differs from upstream.** SAM-Road's naive decoder is four stride-2 transposed convolutions, and the last one outputs 2 channels. `OriginalNaiveDecoder` makes the last one output 32 channels with a GELU. terratorch's segmentation head then adds a 1 by 1 convolution to 2 channels. Four stride-2 steps take 16 by 16 to 256 by 256.
- **TopoNet reads the encoder output for transformer variants.** The wrappers keep the last encoder output in `final_embed`, and `_segment` reads it after the forward pass. The decoder's maps are not used for point features.
- **TopoNet reads a pyramid for CNN variants.** For U-Net, the point features are the encoder's deepest map plus the output of each of the 5 decoder blocks: 512, 256, 128, 64, 32 and 16 channels at 8 px up to 256 px. For SGCN, they are the GCN block output and the three decoder outputs: 512, 256, 128 and 64 channels at 33 px up to 256 px. Forward hooks in `RoadSegmentationModel._register_feature_maps` capture these maps.
- **ROSA norm stats.** `rosa_norm` uses frozen train means and stds of the raw-labels baseline v2 (21 bands), keyed by band name. A std below 1e-6 becomes 1.0.
- **TopoNet sizes are fixed.** The hidden width is 128, with 4 heads, 3 layers and dropout 0.1. Each crop gets `TOPO_SAMPLE_NUM` 128 source points with up to `MAX_NEIGHBOR_QUERIES` 16 pairs each. Padded pairs are masked in attention. When a sample has no valid pair, the mask is flipped to all valid, so attention does not return NaN.
- **Mask loss.** `MASK_LOSS` is `bce_dice` in every config. `BCEDiceLoss` adds a mean BCE over both channels to a multilabel Dice loss with smoothing 1.0. The keypoint and road channels get equal weight. `bce` alone is the other option.
- **Topology loss.** It is a BCE per pair, multiplied by the valid mask and divided by the number of valid pairs. The count is clamped to at least 1.
- **Changes to the copied TopoNet.** The `TopoNet` in `model.py` is a copy of the upstream one with two edits. `no_tgt_features` and `no_offset` are now an `if` and `elif`. Upstream used two `if`s, so its `no_tgt_features` ablation was overwritten by the `else` branch. The transformer is built with `enable_nested_tensor=False`. Every config uses `TOPONET_VERSION: normal`, so the ablations do not run.
- **What is dropped from SAM-Road.** The SAM mask decoder option, LoRA, encoder freezing and focal loss are gone. The comments "Removed Freeze encoder" and "Removed LORA" in `configure_optimizers` mark two of them.

## Why it's like this

- **Bicubic upsampling.** The README abstract says the upsampling addresses the lower resolution of Sentinel-2. The choice of a 128 px crop and a factor of 2 has no recorded reason. Reason unknown, see KEL-30.
- **Backbone and CNN swaps.** The abstract says these changes address the extra bands of Sentinel-2. It reports 49.79 APLS, which is 2.69 APLS over the SAM-Road baseline, by using extra bands.
- **SAM stays RGB only.** No code adapts SAM's patch embedding to more bands. SAM is the baseline in the abstract. Why the extra bands were not fed to SAM itself is not recorded. Reason unknown, see KEL-31.
- **terratorch wrapping.** The transformer variants go through terratorch's factory, which adds a neck and a head around the SAM-Road decoder. Reason unknown, see KEL-32.
- **BCE plus Dice.** Every config uses `bce_dice` with equal weights, where upstream used BCE or focal loss. Reason unknown, see KEL-33.

## Weak spots and ideas

- **Observed. `RGB_SOURCE` is ignored by the CNN norm stats.** `RoadSegmentationModel` calls `resolve_input` with `"enhanced"` hard-coded. The dataset uses `RGB_SOURCE`. With `RGB_SOURCE: raw`, a U-Net or SGCN would get raw RGB normalised with enhanced RGB stats. `sam_norm` and `terramind_norm` also assume values from 0 to 1 times 255, which fits enhanced RGB only.
- **Observed. The ImageNet U-Net gets ROSA stats, not ImageNet stats.** The pretrained first layer sees inputs scaled unlike its pretraining. Nobody has tested whether this matters.
- **Observed. SGCN ignores `PRETRAINED`.** It always starts from random weights, while `sgcn.yaml` sets `PRETRAINED: true`. It is the only variant with no pretraining, which muddies the comparison.
- **Observed. Point features differ by variant.** SAM and TerraMind give TopoNet one 16 by 16 map, which is one vector per 16 px. The CNNs give full-resolution pyramids of about 1000 channels. Differences in topology scores may come from this and not from the backbone.
- **Observed. Different ways to get the feature maps.** The transformer wrappers store `final_embed` as state on the module, which `_segment` reads after the call. The CNN wrappers use forward hooks. One return value from each wrapper would remove both side channels.
- **Suspected. `resize_sam_pos_embed` matches block numbers as substrings.** It picks global blocks with `str(i) in k`. For ViT-B with blocks 2, 5, 8 and 11 this works. For ViT-L, block 15 contains "5" and would get a wrongly sized table. The code is copied from upstream.
- **Observed. Duplicated code.** `TopoNet`, `resize_sam_pos_embed` and the SAM size table exist in both `models/model.py` and `sam_road/model.py`. `COMPLETE_SEG_ENCODERS` is defined in `utils.py` and in `dataset/bands.py`.
- **Observed. Hard-coded values.** `topo_feature_dim = 256` for SAM is a literal and is not read from the encoder. The TopoNet sizes are not in the config.
- **Observed. `GeoSAMRoad` does too much.** One class builds the backbone, loads and resizes the SAM checkpoint, holds 13 metric objects, logs W&B images and finds best thresholds. The `training_step`, `validation_step` and `test_step` methods repeat the loss code. A backbone factory and a separate metrics module would shrink it.
- **Observed. Dead code.** `CHANNELS` in `unet_model.py`, `VIT_PATCH_SIZE` in `geospatial_encoder.py` and `TM_PRETRAINED_BANDS` for `sen2l1c` are never read. `SCGN_model.py` imports Lightning, smp and torchmetrics and uses none of them. From `sam_road/model.py` our code imports only `BilinearSampler`. The `SAMRoad` class there and `sam_road/vitdet.py` run only from the upstream scripts in `sam_road/`. Leftover `SAM_VERSION` and `SAM_CKPT_PATH` keys sit in every non-SAM config.
- **Observed. The file is named `SCGN_model.py`** for a model called SGCN.
- **Observed. Little test coverage.** `tests/geosamroad/test_band_selection.py` tests band selection. No test builds a model or checks a feature dimension.

## Code map

- `models/model.py`, `GeoSAMRoad`: builds the chosen backbone, runs segmentation and TopoNet, computes losses and metrics, and sets up the optimiser.
- `models/model.py`, `TopoNet`: scores candidate links between point pairs.
- `models/samroad_wrappers.py`, `TerraTorchSAMWrapper` and `OriginalNaiveDecoder`: the SAM encoder in terratorch form and the SAM-Road decoder.
- `models/geospatial_encoder.py`, `GeoBackboneEncoder`: the TerraMind backbone and the per-modality band split.
- `models/segmentation_wrappers.py`, `UnetRoadSegmentation` and `SgcnRoadSegmentation`: the CNN variants, their normalisation and their feature-map hooks.
- `models/unet_model.py`, `build_model`: the smp U-Net.
- `models/SCGN_model.py`, `SGCN_res50`: the vendored SGCN network.
- `models/normalization.py`, `sam_norm`, `terramind_norm` and `rosa_norm`: the norm stats per backbone.
- `models/losses.py`, `build_mask_criterion` and `BCEDiceLoss`: the mask loss.
- `dataset/bands.py`, `resolve_input` and `encoder_modalities`: which bands each variant reads.
- `sam_road/model.py`, `BilinearSampler`: the only upstream symbol the model imports.

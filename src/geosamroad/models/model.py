import torch
import torch.nn.functional as F
from torch import nn
from functools import partial
from torchmetrics.classification import (
    BinaryJaccardIndex, 
    F1Score, 
    BinaryPrecisionRecallCurve,
    BinaryF1Score, 
    BinaryPrecision, 
    BinaryRecall
)
import lightning.pytorch as pl
from segment_anything.modeling.image_encoder import ImageEncoderViT
from terratorch.models import EncoderDecoderFactory
from geosamroad.models.samroad_wrappers import TerraTorchSAMWrapper, OriginalNaiveDecoder
from geosamroad.models.normalization import terramind_norm, sam_norm
from geosamroad.models.geospatial_encoder import GeoBackboneEncoder
from geosamroad.models.segmentation_wrappers import UnetRoadSegmentation, SgcnRoadSegmentation
from geosamroad.models.losses import build_mask_criterion
from geosamroad.sam_road.model import BilinearSampler

import wandb
import numpy as np


def _stretch_rgb_for_log(rgb, percentile_range=(2, 98)):
    """Raw band float image to rgb with cummulative stretch"""
    rgb = np.asarray(rgb, dtype=np.float32)
    out = np.zeros(rgb.shape, dtype=np.uint8)
    for c in range(rgb.shape[-1]):
        band = rgb[..., c]
        finite = band[np.isfinite(band)]
        if finite.size == 0:
            continue
        lo, hi = np.percentile(finite, percentile_range)
        if hi <= lo:
            continue
        out[..., c] = (np.clip(band, lo, hi) - lo) / (hi - lo) * 255.0
    return out

## COPIED FROM SAMROAD
class TopoNet(nn.Module):
    def __init__(self, config, feature_dim):
        super(TopoNet, self).__init__()
        self.config = config

        self.hidden_dim = 128
        self.heads = 4
        self.num_attn_layers = 3

        self.feature_proj = nn.Linear(feature_dim, self.hidden_dim)
        self.pair_proj = nn.Linear(2 * self.hidden_dim + 2, self.hidden_dim)

        # Create Transformer Encoder Layer
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=self.hidden_dim,
            nhead=self.heads,
            dim_feedforward=self.hidden_dim,
            dropout=0.1,
            activation='relu',
            batch_first=True  # Input format is [batch size, sequence length, features]
        )
        
        # Stack the Transformer Encoder Layers
        if self.config.TOPONET_VERSION != 'no_transformer':
            self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=self.num_attn_layers, enable_nested_tensor=False)
        self.output_proj = nn.Linear(self.hidden_dim, 1)

    def forward(self, points, point_features, pairs, pairs_valid):
        # points: [B, N_points, 2]
        # point_features: [B, N_points, D]
        # pairs: [B, N_samples, N_pairs, 2]
        # pairs_valid: [B, N_samples, N_pairs]
        
        point_features = F.relu(self.feature_proj(point_features))
        # gathers pairs
        batch_size, n_samples, n_pairs, _ = pairs.shape
        pairs = pairs.view(batch_size, -1, 2)
        
        batch_indices = torch.arange(batch_size).view(-1, 1).expand(-1, n_samples * n_pairs)
        # Use advanced indexing to fetch the corresponding feature vectors
        # [B, N_samples * N_pairs, D]
        src_features = point_features[batch_indices, pairs[:, :, 0]]
        tgt_features = point_features[batch_indices, pairs[:, :, 1]]
        # [B, N_samples * N_pairs, 2]
        src_points = points[batch_indices, pairs[:, :, 0]]
        tgt_points = points[batch_indices, pairs[:, :, 1]]
        offset = tgt_points - src_points

        ## ablation study
        # [B, N_samples * N_pairs, 2D + 2]
        if self.config.TOPONET_VERSION == 'no_tgt_features':
            pair_features = torch.concat([src_features, torch.zeros_like(tgt_features), offset], dim=2)
        elif self.config.TOPONET_VERSION == 'no_offset':
            pair_features = torch.concat([src_features, tgt_features, torch.zeros_like(offset)], dim=2)
        else:
            pair_features = torch.concat([src_features, tgt_features, offset], dim=2)
        
        
        # [B, N_samples * N_pairs, D]
        pair_features = F.relu(self.pair_proj(pair_features))
        
        # attn applies within each local graph sample
        pair_features = pair_features.view(batch_size * n_samples, n_pairs, -1)
        # valid->not a padding
        pairs_valid = pairs_valid.view(batch_size * n_samples, n_pairs)

        # [B * N_samples, 1]
        #### flips mask for all-invalid pairs to prevent NaN
        all_invalid_pair_mask = torch.eq(torch.sum(pairs_valid, dim=-1), 0).unsqueeze(-1)
        pairs_valid = torch.logical_or(pairs_valid, all_invalid_pair_mask)

        padding_mask = ~pairs_valid
        
        ## ablation study
        if self.config.TOPONET_VERSION != 'no_transformer':
            pair_features = self.transformer_encoder(pair_features, src_key_padding_mask=padding_mask)
        
        ## Seems like at inference time, the returned n_pairs heres might be less - it's the
        # max num of valid pairs across all samples in the batch
        _, n_pairs, _ = pair_features.shape
        pair_features = pair_features.view(batch_size, n_samples, n_pairs, -1)

        # [B, N_samples, N_pairs, 1]
        logits = self.output_proj(pair_features)

        scores = torch.sigmoid(logits)

        return logits, scores

class GeoSAMRoad(pl.LightningModule):
    """This is the RelationFormer module that performs object detection"""
    def __init__(self, config):
        super().__init__()
        self.config = config
        if config.SAM_VERSION == 'vit_b':
            ### SAM config (B)
            encoder_embed_dim=768
            encoder_depth=12
            encoder_num_heads=12
            encoder_global_attn_indexes=[2, 5, 8, 11]
            ###
        elif config.SAM_VERSION == 'vit_l':
            ### SAM config (L)
            encoder_embed_dim=1024
            encoder_depth=24
            encoder_num_heads=16
            encoder_global_attn_indexes=[5, 11, 17, 23]
            ###
        elif config.SAM_VERSION == 'vit_h':
            ### SAM config (H)
            encoder_embed_dim=1280
            encoder_depth=32
            encoder_num_heads=16
            encoder_global_attn_indexes=[7, 15, 23, 31]
            ###
        elif config.ENCODER_MODEL == 'sam':
            raise ValueError(f"Unknown SAM version {config.SAM_VERSION}")

        prompt_embed_dim = 256
        # SAM default is 1024
        image_size = config.PATCH_SIZE
        self.image_size = image_size
        vit_patch_size = 16
        image_embedding_size = image_size // vit_patch_size
        encoder_output_dim = prompt_embed_dim

        ### Backbone Specification
        self.road_seg = None # CNN. Replaces the whole road segmentation component
        self.segmentation_model = None # Transformer. Only replace the encoder

        backbone = None
        if self.config.ENCODER_MODEL == 'sam':
            ### SAM vitb
            orig_sam_encoder = ImageEncoderViT(
                depth=encoder_depth,
                embed_dim=encoder_embed_dim,
                img_size=image_size,
                mlp_ratio=4,
                norm_layer=partial(torch.nn.LayerNorm, eps=1e-6),
                num_heads=encoder_num_heads,
                patch_size=vit_patch_size,
                qkv_bias=True,
                use_rel_pos=True,
                global_attn_indexes=encoder_global_attn_indexes,
                window_size=14,
                out_chans=prompt_embed_dim
            )

            ## Load checkpoint
            if config.get("PRETRAINED", True) and not config.SAM_CKPT_PATH:
                raise ValueError(
                    "PRETRAINED is true but SAM_CKPT_PATH is not set "
                    "(--sam-ckpt-path or --set SAM_CKPT_PATH=...)"
                )
            if config.get("PRETRAINED", True):
                with open(config.SAM_CKPT_PATH, "rb") as f:
                    ckpt_state_dict = torch.load(f)

                    ## Resize pos embeddings, if needed
                    if image_size != 1024:
                        new_state_dict = self.resize_sam_pos_embed(ckpt_state_dict, image_size, vit_patch_size, encoder_global_attn_indexes)
                        ckpt_state_dict = new_state_dict

                    sam_keys_dict = {
                        k.replace("image_encoder.", ""): v
                        for k, v in ckpt_state_dict.items()
                        if k.startswith("image_encoder.")
                    }

                    msg = orig_sam_encoder.load_state_dict(sam_keys_dict, strict=False)
                    print("###### SAM Checkpoint Encoder Status ######")
                    print(f"Missing keys: {len(msg.missing_keys)}, Unexpected keys: {len(msg.unexpected_keys)}")
            
            backbone = TerraTorchSAMWrapper(orig_sam_encoder)
            mean, std, scale = sam_norm()
            topo_feature_dim = 256
        elif self.config.ENCODER_MODEL == 'terramind':
            backbone = GeoBackboneEncoder(str(config.get("TERRAMIND_VERSION", "small")),
                                          self.config.RGB_INPUT, image_size,
                                          pretrained=bool(config.get("PRETRAINED", True)))
            mean, std, scale = terramind_norm(self.config.RGB_INPUT)
            topo_feature_dim = backbone.embed_dim # 384 for small, 768 for base
        elif self.config.ENCODER_MODEL == 'unet':
            self.road_seg = UnetRoadSegmentation(config)
            topo_feature_dim = self.road_seg.topo_feature_dim
        elif self.config.ENCODER_MODEL == 'sgcn':
            self.road_seg = SgcnRoadSegmentation(config)
            topo_feature_dim = self.road_seg.topo_feature_dim
        else:
            raise ValueError(f"Unknown encoder model {self.config.ENCODER_MODEL}")

        if self.road_seg is None:
            self.register_buffer("pixel_mean", torch.Tensor(mean).view(-1, 1, 1), False)
            self.register_buffer("pixel_std", torch.Tensor(std).view(-1, 1, 1), False)
            self.register_buffer("pixel_scale", torch.Tensor([scale]).view(-1, 1, 1), False)

            if backbone is None:
                raise ValueError("Encoder Backbone is not initialised")

            self.segmentation_model = self._build_backbone_segmentation(
                backbone, topo_feature_dim)

        #### TOPONet
        self.bilinear_sampler = BilinearSampler(config)
        self.topo_net = TopoNet(config, topo_feature_dim)

        #### Losses
        self.mask_criterion = build_mask_criterion(config)
        self.topo_criterion = torch.nn.BCEWithLogitsLoss(reduction='none')
        #### Metrics
        self.keypoint_iou = BinaryJaccardIndex(threshold=0.5)
        self.topo_f1 = F1Score(task='binary', threshold=0.5, ignore_index=-1)
        self.road_iou = BinaryJaccardIndex(threshold=0.5)
        self.road_f1 = BinaryF1Score(threshold=0.5)
        self.road_precision = BinaryPrecision(threshold=0.5)
        self.road_recall = BinaryRecall(threshold=0.5)
        self.train_keypoint_iou = BinaryJaccardIndex(threshold=0.5)
        self.train_keypoint_f1 = BinaryF1Score(threshold=0.5)
        self.train_road_iou = BinaryJaccardIndex(threshold=0.5)
        self.train_road_f1 = BinaryF1Score(threshold=0.5)
        # testing only, not used in training. Binned
        self.keypoint_pr_curve = BinaryPrecisionRecallCurve(thresholds=256, ignore_index=-1)
        self.road_pr_curve = BinaryPrecisionRecallCurve(thresholds=256, ignore_index=-1)
        self.topo_pr_curve = BinaryPrecisionRecallCurve(thresholds=256, ignore_index=-1)

    def _build_backbone_segmentation(self, backbone, decoder_in_channels):
        """ SAM-Road Transformer Backbone with naive decoder wrapped in terratorch"""
        ### Road Segmentation
        model_factory = EncoderDecoderFactory()

        return model_factory.build_model(
            task="segmentation",
            backbone=backbone,
            decoder=OriginalNaiveDecoder(in_channels=decoder_in_channels),
            necks=[{"name": "ReshapeTokensToImage", "remove_cls_token": False}],
            num_classes=2, # keypoint, road
            decoder_kwargs={}, # Arguments are handled inside OriginalNaiveDecoder
            image_size_out=(self.image_size, self.image_size),
            rescale=True
        )

    def resize_sam_pos_embed(self, state_dict, image_size, vit_patch_size, encoder_global_attn_indexes):
        new_state_dict = {k : v for k, v in state_dict.items()}
        pos_embed = new_state_dict['image_encoder.pos_embed']
        token_size = int(image_size // vit_patch_size)
        if pos_embed.shape[1] != token_size:
            # Copied from SAMed
            # resize pos embedding, which may sacrifice the performance, but I have no better idea
            pos_embed = pos_embed.permute(0, 3, 1, 2)  # [b, c, h, w]
            pos_embed = F.interpolate(pos_embed, (token_size, token_size), mode='bilinear', align_corners=False)
            pos_embed = pos_embed.permute(0, 2, 3, 1)  # [b, h, w, c]
            new_state_dict['image_encoder.pos_embed'] = pos_embed
            rel_pos_keys = [k for k in state_dict.keys() if 'rel_pos' in k]
            global_rel_pos_keys = [k for k in rel_pos_keys if any([str(i) in k for i in encoder_global_attn_indexes])]
            for k in global_rel_pos_keys:
                rel_pos_params = new_state_dict[k]
                h, w = rel_pos_params.shape
                rel_pos_params = rel_pos_params.unsqueeze(0).unsqueeze(0)
                rel_pos_params = F.interpolate(rel_pos_params, (token_size * 2 - 1, w), mode='bilinear', align_corners=False)
                new_state_dict[k] = rel_pos_params[0, 0, ...]
        return new_state_dict

    def _bilinear_sample_feature_map(self, topo_embed, graph_points):
        """Point features from backbone map, or from a pyramid of them."""
        if isinstance(topo_embed, (list, tuple)):
            return torch.cat(
                [self.bilinear_sampler(f, graph_points) for f in topo_embed], dim=-1)
        return self.bilinear_sampler(topo_embed, graph_points)

    def _segment(self, image):
        """Road segmentation forward pass for logits and feature map embedding"""
        if self.road_seg is not None:
            return self.road_seg(image)      # normalises internally

        x = image * self.pixel_scale
        x = (x - self.pixel_mean) / self.pixel_std
        mask_logits = self.segmentation_model(x).output
        topo_embed = self.segmentation_model.encoder.final_embed
        if topo_embed is None:
            raise RuntimeError("Feature map is not cached")
        return mask_logits, topo_embed

    def forward(self, image, graph_points, pairs, valid):
        # image: [B, C, H, W]
        # graph_points: [B, N_points, 2]
        # pairs: [B, N_samples, N_pairs, 2]
        # valid: [B, N_samples, N_pairs]
        mask_logits, topo_embed = self._segment(image)

        ## Predicts local topology
        point_features = self._bilinear_sample_feature_map(topo_embed, graph_points)
        # [B, N_sample, N_pair, 1]
        topo_logits, topo_scores = self.topo_net(graph_points, point_features, pairs, valid)
        # [B, H, W, 2] -- channel 0 keypoint, channel 1 road
        mask_logits = mask_logits.permute(0, 2, 3, 1)
        mask_scores = torch.sigmoid(mask_logits)
        return mask_logits, mask_scores, topo_logits, topo_scores

    def infer_masks_and_img_features(self, image):
        # image: [B, C, H, W] -> ([B, H, W, 2] scores, the topo feature map(s))
        mask_logits, topo_embed = self._segment(image)
        return torch.sigmoid(mask_logits).permute(0, 2, 3, 1), topo_embed

    def infer_toponet(self, image_embeddings, graph_points, pairs, valid):
        # image_embeddings: [B, D, h, w], or a list of such maps
        # graph_points: [B, N_points, 2]
        # pairs: [B, N_samples, N_pairs, 2]
        # valid: [B, N_samples, N_pairs]

        ## Predicts local topology
        point_features = self._bilinear_sample_feature_map(image_embeddings, graph_points)
        # [B, N_sample, N_pair, 1]
        topo_logits, topo_scores = self.topo_net(graph_points, point_features, pairs, valid)
        return topo_scores

    def training_step(self, batch, batch_idx):
        # masks: [B, H, W]
        image, keypoint_mask, road_mask  = batch['image'], batch['keypoint_mask'], batch['road_mask']
        graph_points, pairs, valid = batch['graph_points'], batch['pairs'], batch['valid']
        # [B, H, W, 2]
        mask_logits, mask_scores, topo_logits, topo_scores = self(image, graph_points, pairs, valid)
        gt_masks = torch.stack([keypoint_mask, road_mask], dim=3)
        mask_loss = self.mask_criterion(mask_logits, gt_masks)
        topo_gt, topo_loss_mask = batch['connected'].to(torch.int32), valid.to(torch.float32)
        # [B, N_samples, N_pairs, 1]
        topo_loss = self.topo_criterion(topo_logits, topo_gt.unsqueeze(-1).to(torch.float32))

        topo_loss *= topo_loss_mask.unsqueeze(-1)
        # topo_loss = torch.nansum(torch.nansum(topo_loss) / topo_loss_mask.sum())
        topo_loss = topo_loss.sum() / topo_loss_mask.sum().clamp(min=1.0)

        loss =  mask_loss + topo_loss

        if torch.isnan(loss):  # skip step
            print(f"NaN loss at step {batch_idx}; skipping this batch "
                  f"(mask={float(mask_loss):.4f} topo={float(topo_loss):.4f})")
            return None
        self.log('train_mask_loss', mask_loss, on_step=True, on_epoch=False, prog_bar=True)
        self.log('train_topo_loss', topo_loss, on_step=True, on_epoch=False, prog_bar=True)
        self.log('train_loss', loss, on_step=True, on_epoch=False, prog_bar=True)

        road_mask_bin = (road_mask > 0.5).float()
        self.train_keypoint_iou.update(mask_scores[..., 0], keypoint_mask)
        self.train_keypoint_f1.update(mask_scores[..., 0], keypoint_mask)
        self.train_road_iou.update(mask_scores[..., 1], road_mask_bin)
        self.train_road_f1.update(mask_scores[..., 1], road_mask_bin)
        self.log_dict({
            'train_keypoint_iou': self.train_keypoint_iou,
            'train_keypoint_f1': self.train_keypoint_f1,
            'train_road_iou': self.train_road_iou,
            'train_road_f1': self.train_road_f1,
        }, on_step=False, on_epoch=True)
        return loss

    def validation_step(self, batch, batch_idx):
        # masks: [B, H, W]
        image, keypoint_mask, road_mask = batch['image'], batch['keypoint_mask'], batch['road_mask']
        graph_points, pairs, valid = batch['graph_points'], batch['pairs'], batch['valid']
       # masks: [B, H, W, 2] topo: [B, N_samples, N_pairs, 1]
        mask_logits, mask_scores, topo_logits, topo_scores = self(image, graph_points, pairs, valid)
        gt_masks = torch.stack([keypoint_mask, road_mask], dim=3)
        mask_loss = self.mask_criterion(mask_logits, gt_masks)
        topo_gt, topo_loss_mask = batch['connected'].to(torch.int32), valid.to(torch.float32)
        # [B, N_samples, N_pairs, 1]
        topo_loss = self.topo_criterion(topo_logits, topo_gt.unsqueeze(-1).to(torch.float32))
        topo_loss *= topo_loss_mask.unsqueeze(-1)
        # topo_loss = topo_loss.sum() / topo_loss_mask.sum()
        topo_loss = topo_loss.sum() / topo_loss_mask.sum().clamp(min=1.0)
        loss = mask_loss + topo_loss
        self.log('val_mask_loss', mask_loss, on_step=False, on_epoch=True, prog_bar=True,
                 sync_dist=True)
        self.log('val_topo_loss', topo_loss, on_step=False, on_epoch=True, prog_bar=True,
                 sync_dist=True)
        self.log('val_loss', loss, on_step=False, on_epoch=True, prog_bar=True,
                 sync_dist=True)
        # Log images
        if batch_idx == 0 and hasattr(self.logger, "log_table"):
            max_viz_num = 4
            # First 3 channels are RGB
            viz_rgb = image[:max_viz_num, :3].permute(0, 2, 3, 1)
            viz_pred_keypoint = mask_scores[:max_viz_num, :, :, 0]
            viz_pred_road = mask_scores[:max_viz_num, :, :, 1]
            viz_gt_keypoint = keypoint_mask[:max_viz_num, ...]
            viz_gt_road = road_mask[:max_viz_num, ...]

            columns = ['rgb', 'gt_keypoint', 'gt_road', 'pred_keypoint', 'pred_road']
            data = []
            for r, gk, gr, pk, pr in zip(viz_rgb, viz_gt_keypoint, viz_gt_road,
                                         viz_pred_keypoint, viz_pred_road):
                data.append([
                    wandb.Image(_stretch_rgb_for_log(r.cpu().numpy())),  # cumulative stretch
                    wandb.Image(gk.cpu().numpy()),
                    wandb.Image(gr.cpu().numpy()),
                    wandb.Image(pk.cpu().numpy()),
                    wandb.Image(pr.cpu().numpy()),
                ])
            self.logger.log_table(key='viz_table', columns=columns, data=data)

        road_mask = (road_mask > 0.5).float()
        self.keypoint_iou.update(mask_scores[..., 0], keypoint_mask)
        self.road_iou.update(mask_scores[..., 1], road_mask)
        self.road_f1.update(mask_scores[..., 1], road_mask)
        self.road_precision.update(mask_scores[..., 1], road_mask)
        self.road_recall.update(mask_scores[..., 1], road_mask)
        
        valid = valid.to(torch.int32)
        topo_gt = (1 - valid) * -1 + valid * topo_gt
        self.topo_f1.update(topo_scores, topo_gt.unsqueeze(-1))
        
    def on_validation_epoch_end(self):
        keypoint_iou = self.keypoint_iou.compute()
        road_iou = self.road_iou.compute()
        road_f1 = self.road_f1.compute()
        road_precision = self.road_precision.compute()
        road_recall = self.road_recall.compute()
        topo_f1 = self.topo_f1.compute()
        self.log("keypoint_iou", keypoint_iou)
        self.log("road_iou", road_iou)
        self.log("road_f1", road_f1)
        self.log("road_precision", road_precision)
        self.log("road_recall", road_recall)
        self.log("topo_f1", topo_f1)  
        self.keypoint_iou.reset()
        self.road_iou.reset()
        self.road_f1.reset()
        self.road_precision.reset()
        self.road_recall.reset()
        self.topo_f1.reset()

    def test_step(self, batch, batch_idx):
        # masks: [B, H, W]
        image, keypoint_mask, road_mask = batch['image'], batch['keypoint_mask'], batch['road_mask']
        graph_points, pairs, valid = batch['graph_points'], batch['pairs'], batch['valid']
        # masks: [B, H, W, 2] topo: [B, N_samples, N_pairs, 1]
        mask_logits, mask_scores, topo_logits, topo_scores = self(image, graph_points, pairs, valid)
        topo_gt, topo_loss_mask = batch['connected'].to(torch.int32), valid.to(torch.float32)
        self.keypoint_pr_curve.update(mask_scores[..., 0], keypoint_mask.to(torch.int32))
        self.road_pr_curve.update(mask_scores[..., 1], road_mask.to(torch.int32))
        # fixed-threshold (0.5) road metrics, reported in the test results table
        road_mask_bin = (road_mask > 0.5).float()
        self.road_iou.update(mask_scores[..., 1], road_mask_bin)
        self.road_f1.update(mask_scores[..., 1], road_mask_bin)
        self.road_precision.update(mask_scores[..., 1], road_mask_bin)
        self.road_recall.update(mask_scores[..., 1], road_mask_bin)
        valid = valid.to(torch.int32)
        topo_gt = (1 - valid) * -1 + valid * topo_gt
        self.topo_pr_curve.update(topo_scores, topo_gt.unsqueeze(-1).to(torch.int32))

    def on_test_epoch_end(self):
        self.log("test_road_iou", self.road_iou.compute())
        self.log("test_road_f1", self.road_f1.compute())
        self.log("test_road_precision", self.road_precision.compute())
        self.log("test_road_recall", self.road_recall.compute())
        self.road_iou.reset()
        self.road_f1.reset()
        self.road_precision.reset()
        self.road_recall.reset()

    def on_test_end(self):
        def find_best_threshold(pr_curve_metric, category):
            print(f'======= {category} ======')
            precision, recall, thresholds = pr_curve_metric.compute()
            f1_scores = 2 * (precision * recall) / (precision + recall)
            f1_scores = torch.nan_to_num(f1_scores[:len(thresholds)], nan=0.0)
            best_threshold_index = torch.argmax(f1_scores)
            best_threshold = thresholds[best_threshold_index]
            best_precision = precision[best_threshold_index]
            best_recall = recall[best_threshold_index]
            best_f1 = f1_scores[best_threshold_index]
            print(f'Best threshold {best_threshold}, P={best_precision} R={best_recall} F1={best_f1}')
        print('======= Finding best thresholds ======')
        find_best_threshold(self.keypoint_pr_curve, 'keypoint')
        find_best_threshold(self.road_pr_curve, 'road')
        find_best_threshold(self.topo_pr_curve, 'topo')

    def configure_optimizers(self):
        param_dicts = []

        if self.road_seg is not None: # CNN
            param_dicts += self.road_seg.param_groups(
                self.config.BASE_LR, self.config.get("ENCODER_LR_FACTOR", 1.0))
        else:
            ## Removed Freeze encoder
            encoder_params = { # Transformer
                'params': list(self.segmentation_model.encoder.parameters()),
                'lr': self.config.BASE_LR * self.config.ENCODER_LR_FACTOR,
            }
            param_dicts.append(encoder_params)

            ## Removed LORA

            ### Decoder params
            decoder_params = {
                'params': [p for k, p in self.segmentation_model.named_parameters() if not k.startswith('encoder.')],
                'lr': self.config.BASE_LR
            }
            param_dicts.append(decoder_params)

        ### TOPO params
        topo_net_params = [{
            'params': [p for p in self.topo_net.parameters()],
            'lr': self.config.BASE_LR
        }]
        param_dicts += topo_net_params
        param_dicts = [g for g in param_dicts if g['params']]

        for i, param_dict in enumerate(param_dicts):
            param_num = sum([int(p.numel()) for p in param_dict['params']])
            print(f'optim param dict {i} params num: {param_num}')

        weight_decay = self.config.get("WEIGHT_DECAY", 0.0)
        optimizer = torch.optim.AdamW(param_dicts, lr=self.config.BASE_LR, betas=(0.9, 0.999), weight_decay=weight_decay)
        # optimizer = torch.optim.Adam(param_dicts, lr=self.config.BASE_LR)

        milestones = self.config.get("LR_MILESTONES", [9,])
        gamma = self.config.get("LR_GAMMA", 0.1)
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=milestones, gamma=gamma)

        return {'optimizer': optimizer, 'lr_scheduler': scheduler}
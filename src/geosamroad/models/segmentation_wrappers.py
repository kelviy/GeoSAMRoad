import torch
from torch import nn

from geosamroad.dataset.bands import HIGH_RES_SOURCE, resolve_input
from geosamroad.models.normalization import high_res_rgb_norm, rosa_norm

MASK_CLASSES = 2  # (keypoint, road)
SGCN_FEATURE_MAPS = ("gcn_out", "up1", "up2", "up3")

class RoadSegmentationModel(nn.Module):
    def __init__(self, config, topo_feature_dim_fn, build_net):
        super().__init__()
        self.config = config
        rgb_source = str(config.get("RGB_SOURCE") or "enhanced")
        bands = resolve_input(str(config.ENCODER_MODEL), bool(config.RGB_INPUT), rgb_source)
        # the high-res aerial tiles are their own raster with their own stats,
        # not bands of the 23-band ROSA stack
        mean, std, scale = (high_res_rgb_norm() if rgb_source == HIGH_RES_SOURCE
                            else rosa_norm(bands))
        self.bands = tuple(bands)
        self.in_channels = len(bands)
        self.net = build_net(self.in_channels)
        self.topo_feature_dim = int(topo_feature_dim_fn(self.net))

        # normalisation stats
        for name, value in (("pixel_mean", mean), ("pixel_std", std),
                            ("pixel_scale", scale)):
            tensor = torch.as_tensor(value, dtype=torch.float32).reshape(-1, 1, 1)
            if tensor.numel() not in (1, self.in_channels):
                raise ValueError(
                    f"{name} has {tensor.numel()} entries but the model reads "
                    f"{self.in_channels} channels"
                )

            self.register_buffer(name, tensor, persistent=False)

        self._feature_map_names: tuple[str, ...] = ()
        self._feature_maps: dict[str, torch.Tensor | None] = {}

    def normalize(self, image):
        return (image * self.pixel_scale - self.pixel_mean) / self.pixel_std # [B, C, H, W]

    def _register_feature_maps(self, modules):
        """Capturing feature maps during forward pass"""
        self._feature_map_names = tuple(modules)
        self._feature_maps = {name: None for name in self._feature_map_names}
        for name, module in modules.items():
            module.register_forward_hook(
                lambda _module, _inputs, output, name=name: self._feature_maps.__setitem__(name, output)
            )

    def _clear_feature_maps(self):
        self._feature_maps = {name: None for name in self._feature_map_names}

    def _collect_feature_maps(self):
        """Obtain feature maps in a list"""
        missing = [name for name in self._feature_map_names if self._feature_maps.get(name) is None]
        if missing:
            raise RuntimeError(
                f"{type(self).__name__}: forward hooks did not fire for {missing}; "
                f"the network's forward changed"
            )
        maps = [self._feature_maps[name] for name in self._feature_map_names]
        self._clear_feature_maps()
        return maps

    def forward(self, image):
        raise NotImplementedError

    def param_groups(self, base_lr, encoder_lr_factor):
        """Optimiser groups for this module."""
        raise NotImplementedError


def _single_group(module, base_lr):
    params = [p for p in module.parameters() if p.requires_grad]
    return [{"params": params, "lr": base_lr}] if params else []


def _split_groups(module, encoder, base_lr, encoder_lr_factor):
    """Enables training of pretrained Encoder at a reduced LR from base LR."""
    enc = [p for p in encoder.parameters() if p.requires_grad]
    enc_ids = {id(p) for p in enc}
    rest = [p for p in module.parameters()
            if p.requires_grad and id(p) not in enc_ids]
    groups = [{"params": enc, "lr": base_lr * encoder_lr_factor},
              {"params": rest, "lr": base_lr}]
    return [g for g in groups if g["params"]]


class UnetRoadSegmentation(RoadSegmentationModel):
    def __init__(self, config):
        from geosamroad.models.unet_model import build_model # lazy init

        weights = config.get("SEG_ENCODER_WEIGHTS", "imagenet")
        
        if not config.get("PRETRAINED", True):
            weights = None

        super().__init__(
            config,
            # read feature dims
            topo_feature_dim_fn=lambda net: (
                net.encoder.out_channels[-1]
                + sum(b.conv2[0].out_channels for b in net.decoder.blocks)
            ),
            build_net=lambda in_channels: build_model(
                encoder_name=config.get("SEG_ENCODER_NAME", "resnet34"),
                encoder_weights=weights,
                in_channels=in_channels,
                classes=MASK_CLASSES,
            ),
        )
        self._register_feature_maps({
            f"decoder_block_{i}": block
            for i, block in enumerate(self.net.decoder.blocks)
        })

    def forward(self, image):
        x = self.normalize(image)
        self.net.check_input_shape(x)
        self._clear_feature_maps()
        features = self.net.encoder(x)
        mask_logits = self.net.segmentation_head(self.net.decoder(features))
        topo_embed = [features[-1]] + self._collect_feature_maps()
        return mask_logits, topo_embed

    def param_groups(self, base_lr, encoder_lr_factor):
        return _split_groups(self.net, self.net.encoder, base_lr, encoder_lr_factor)


class SgcnRoadSegmentation(RoadSegmentationModel):
    def __init__(self, config):
        from geosamroad.models.SCGN_model import SGCN_res50

        super().__init__(
            config,
            topo_feature_dim_fn=lambda net: sum(
                net.gcn_out.output[-1].out_channels if name == "gcn_out"
                else getattr(net, name).up.out_channels
                for name in SGCN_FEATURE_MAPS
            ),
            build_net=lambda in_channels: SGCN_res50(
                num_classes=MASK_CLASSES, in_channels=in_channels),
        )
        self._register_feature_maps({name: getattr(self.net, name) for name in SGCN_FEATURE_MAPS})
        if float(config.get("ENCODER_LR_FACTOR", 1.0)) != 1.0:
            print(f"###### ENCODER_MODEL: sgcn has no encoder/decoder split; "
                  f"ENCODER_LR_FACTOR={config.get('ENCODER_LR_FACTOR')} is ignored ######")

    def forward(self, image):
        self._clear_feature_maps()
        mask_logits = self.net(self.normalize(image))
        return mask_logits, self._collect_feature_maps()

    def param_groups(self, base_lr, encoder_lr_factor):
        return _single_group(self.net, base_lr)
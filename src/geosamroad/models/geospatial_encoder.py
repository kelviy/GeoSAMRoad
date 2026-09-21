from collections import OrderedDict
import torch
from torch import nn
from terratorch.registry import BACKBONE_REGISTRY

from geosamroad.dataset.bands import encoder_modalities

REGISTRY_NAME = {
    "small": "terramind_v1_small",
    "base": "terramind_v1_base",
}

VIT_PATCH_SIZE = 16


class GeoBackboneEncoder(nn.Module):
    """TerraMind encoder backbone"""

    def __init__(self, version: str, rgb: bool, img_size: int, pretrained: bool = True):
        super().__init__()
        if version not in REGISTRY_NAME:
            raise ValueError(
                f"Unknown TERRAMIND_VERSION {version!r}; expected one of "
                f"{list(REGISTRY_NAME)}"
            )
        self.version = version
        self.img_size = img_size
        mods = encoder_modalities("terramind", rgb)

        self.backbone = BACKBONE_REGISTRY.build(
            REGISTRY_NAME[version], pretrained=pretrained, img_size=img_size,
            modalities=list(mods), bands=dict(mods),
            merge_method="mean",  # mean(default) | max | concat (T = (H/16)^2)
        )
        # Split stack to bands per satellite modality.
        self.slices: OrderedDict[str, slice] = self._channel_slices(mods)

        # read the width off out_channels.
        self.embed_dim: int = int(self.backbone.out_channels[-1])

        self.out_channels: list[int] = [self.embed_dim]
        self.final_embed: torch.Tensor | None = None

    @staticmethod
    def _channel_slices(modalities: OrderedDict[str, list[str]]) -> OrderedDict[str, slice]:
        slices: OrderedDict[str, slice] = OrderedDict()
        start = 0
        for mod, bands in modalities.items():
            slices[mod] = slice(start, start + len(bands))
            start += len(bands)
        return slices

    def forward(self, x: torch.Tensor) -> list[torch.Tensor]:
        x = {mod: x[:, sl] for mod, sl in self.slices.items()} 
        last = self.backbone(x)[-1]

        b, t, d = last.shape
        hw = round(t ** 0.5)
        if hw * hw != t:
            raise ValueError(
                f"Token count {t} is not a perfect square; cannot reshape to a "
                f"square feature map (version={self.version!r})."
            )
        # [B, T, D] to [B, D, T] to [B, D, hw, hw]
        self.final_embed = last.transpose(1, 2).reshape(b, d, hw, hw)
        return [self.final_embed]

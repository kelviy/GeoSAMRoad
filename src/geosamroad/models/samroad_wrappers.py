import torch
from torch import nn
from segment_anything.modeling.image_encoder import ImageEncoderViT
from segment_anything.modeling.common import LayerNorm2d

class TerraTorchSAMWrapper(nn.Module):
    """Wraps SAM Encoder"""
    def __init__(self, sam_encoder: ImageEncoderViT):
        super().__init__()
        self.sam_encoder = sam_encoder

        self.out_channels = [sam_encoder.neck[0].out_channels]
        self.final_embed = None

    def forward(self, x):
        self.final_embed = self.sam_encoder(x)
        return [self.final_embed]


class OriginalNaiveDecoder(nn.Module):
    """Wraps the original Sam Road custom Naive Decoder"""
    def __init__(self, in_channels: int):
        super().__init__()
        activation = nn.GELU
        
        self.map_decoder = nn.Sequential(
            nn.ConvTranspose2d(in_channels, 128, kernel_size=2, stride=2),
            LayerNorm2d(128),
            activation(),
            nn.ConvTranspose2d(128, 64, kernel_size=2, stride=2),
            activation(),
            nn.ConvTranspose2d(64, 32, kernel_size=2, stride=2),
            activation(),
            nn.ConvTranspose2d(32, 32, kernel_size=2, stride=2),
            activation()
        )

        self.out_channels = 32

    def forward(self, x: list[torch.Tensor]) -> torch.Tensor:
        feat = x[-1]
        return self.map_decoder(feat)
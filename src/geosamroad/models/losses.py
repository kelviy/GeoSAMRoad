from torch import nn
import segmentation_models_pytorch as smp

class BCEDiceLoss(nn.Module):
    """``BCE + soft-Dice``, equally weighted."""

    def __init__(self):
        super().__init__()
        self.bce = nn.BCEWithLogitsLoss()

        self.dice = smp.losses.DiceLoss(
            mode=smp.losses.MULTILABEL_MODE,
            from_logits=True,
            smooth=1.0
        )

    def forward(self, logits, targets):
        targets = targets.to(logits.dtype)

        bce_loss = self.bce(logits, targets)

        # permute to expected shape
        logits_c_first = logits.permute(0, 3, 1, 2)
        targets_c_first = targets.permute(0, 3, 1, 2)

        dice_loss = self.dice(logits_c_first, targets_c_first)

        return bce_loss + dice_loss


def build_mask_criterion(config):
    """Build the mask loss named by ``MASK_LOSS``."""
    name = str(config.get("MASK_LOSS") or "bce").lower()

    if name == "bce":
        return nn.BCEWithLogitsLoss()
    if name == "bce_dice":
        return BCEDiceLoss()
    raise ValueError(f"Unknown MASK_LOSS {name!r} (expected bce | bce_dice)")

import segmentation_models_pytorch as smp

CHANNELS = ("keypoint", "road") 

def build_model(encoder_name="resnet34", encoder_weights="imagenet",
                in_channels=12, classes=2):
    if encoder_weights in (None, "none", "None", ""):
        encoder_weights = None
    return smp.Unet(
        encoder_name=encoder_name,
        encoder_weights=encoder_weights,
        in_channels=in_channels,
        classes=classes,
    )
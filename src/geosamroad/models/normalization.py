from geosamroad.dataset.bands import TM_S2L2A_NAMES, TM_S1GRD_NAMES

# SAM RGB stats, for 0-255 pixel values
SAM_PIXEL_MEAN = [123.675, 116.28, 103.53]
SAM_PIXEL_STD = [58.395, 57.12, 57.375]

# Terramind stats
v1_pretraining_mean = {
    'untok_sen2l2a@224': [1390.458, 1503.317, 1718.197, 1853.91, 2199.1, 2779.975, 2987.011, 3083.234, 3132.22, 3162.988, 2424.884, 1857.648],
    'untok_sen2rgb@224': [87.271, 80.931, 66.667],
    'untok_sen1grd@224': [-12.599, -20.293],
    'untok_sen1rtc@224': [-10.93, -17.329],
    'untok_dem@224': [670.665]
}

v1_pretraining_std = {
    'untok_sen2l2a@224': [2106.761, 2141.107, 2038.973, 2134.138, 2085.321, 1889.926, 1820.257, 1871.918, 1753.829, 1797.379, 1434.261, 1334.311],
    'untok_sen2rgb@224': [58.767, 47.663, 42.631],
    'untok_sen1grd@224': [5.195, 5.890],
    'untok_sen1rtc@224': [4.391, 4.459],
    'untok_dem@224': [951.272]
}

TM_PRETRAINED_BANDS = {
    'untok_sen2l2a@224': [
        "COASTAL_AEROSOL",
        "BLUE",
        "GREEN",
        "RED",
        "RED_EDGE_1",
        "RED_EDGE_2",
        "RED_EDGE_3",
        "NIR_BROAD",
        "NIR_NARROW",
        "WATER_VAPOR",
        "SWIR_1",
        "SWIR_2",
    ],
    'untok_sen2l1c@224': [
        "COASTAL_AEROSOL",
        "BLUE",
        "GREEN",
        "RED",
        "RED_EDGE_1",
        "RED_EDGE_2",
        "RED_EDGE_3",
        "NIR_BROAD",
        "NIR_NARROW",
        "WATER_VAPOR",
        "CIRRUS",
        "SWIR_1",
        "SWIR_2",
    ],
    'untok_sen2rgb@224': ["RED", "GREEN", "BLUE"],
    'untok_sen1grd@224': ["VV", "VH"],
    'untok_sen1rtc@224': ["VV", "VH"],
    'untok_dem@224': ["DEM"],
}

## Methods to return the mean/std/scale for each band
## Currently supports enhanced RGB. TODO: have dataset give percentile clip rgb bands. 

def terramind_norm(rgb: bool):
    if rgb:
        return (
            list(v1_pretraining_mean["untok_sen2rgb@224"]), 
            list(v1_pretraining_std["untok_sen2rgb@224"]), 
            255.0
        )
    else:
        # Terramind dictionary band mapping
        s2_native_names = TM_PRETRAINED_BANDS["untok_sen2l2a@224"]
        s2_mean_map = dict(zip(s2_native_names, v1_pretraining_mean["untok_sen2l2a@224"]))
        s2_std_map = dict(zip(s2_native_names, v1_pretraining_std["untok_sen2l2a@224"]))

        s1_native_names = TM_PRETRAINED_BANDS["untok_sen1grd@224"]
        s1_mean_map = dict(zip(s1_native_names, v1_pretraining_mean["untok_sen1grd@224"]))
        s1_std_map = dict(zip(s1_native_names, v1_pretraining_std["untok_sen1grd@224"]))

        # Reorder norm stat values
        target_s2_means = [s2_mean_map[band] for band in TM_S2L2A_NAMES]
        target_s2_stds = [s2_std_map[band] for band in TM_S2L2A_NAMES]

        target_s1_means = [s1_mean_map[band] for band in TM_S1GRD_NAMES]
        target_s1_stds = [s1_std_map[band] for band in TM_S1GRD_NAMES]

        final_means = target_s2_means + target_s1_means
        final_stds = target_s2_stds + target_s1_stds

        # S2 scaling is 10000.0, S1 (SAR dB) scaling is 1.0
        final_scales = [10000.0] * len(TM_S2L2A_NAMES) + [1.0] * len(TM_S1GRD_NAMES)

        return final_means, final_stds, final_scales


def sam_norm():
    return list(SAM_PIXEL_MEAN), list(SAM_PIXEL_STD), 255

## ROSA Road Dataset Norms
# 23 Bands
ROSA_MEANS=[0.118542, 0.0900151, 0.059118, 0.23897, 0.152557, 0.207308, 0.228655, 0.246422, 0.267869, 0.204559, -12.6379, -19.5885, -11.2967, -18.2211, 690.628, 7.15673, 171.245, 1, 1, 1, 0.551126, 0.522076, 0.48006]
ROSA_STDS=[0.0737832, 0.0416436, 0.0296637, 0.0688163, 0.07177, 0.0614309, 0.0655398, 0.0677955, 0.103342, 0.105481, 3.53385, 3.96946, 3.24179, 3.59768, 471.217, 7.33572, 95.0004, 0, 0, 0, 0.177911, 0.147157, 0.147469]

ROSA_BAND_COUNT = 23


def rosa_norm(bands):
    """Frozen ROSA train stats for unet and sgcn models"""
    if len(ROSA_MEANS) != ROSA_BAND_COUNT or len(ROSA_STDS) != ROSA_BAND_COUNT:
        raise ValueError(
            f"ROSA stats must have {ROSA_BAND_COUNT} entries, got "
            f"{len(ROSA_MEANS)} means / {len(ROSA_STDS)} stds"
        )
    bad = [b for b in bands if not 1 <= int(b) <= ROSA_BAND_COUNT]
    if bad:
        raise ValueError(
            f"ROSA band indices are 1-based in [1, {ROSA_BAND_COUNT}]; got {bad}"
        )
    mean = [float(ROSA_MEANS[int(b) - 1]) for b in bands]
    std = [float(ROSA_STDS[int(b) - 1]) if ROSA_STDS[int(b) - 1] > 1e-6 else 1.0
           for b in bands]
    return mean, std, 1.0

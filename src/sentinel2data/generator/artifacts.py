"""Calculate pixel-coordinate adjacency dict and keypoint masks"""
import numpy as np

# Identify nodes within image edge buffer
BORDER_EPS = 1.0
KEYPOINT_RADIUS = 1


def native_adjacency(roads, transform) -> dict:
    """Convert road centrelines to a pixel coordinates undirected adjecency dict."""
    inv = ~transform
    adj: dict[tuple, set] = {}
    for geom in roads.geometry:
        parts = geom.geoms if geom.geom_type == "MultiLineString" else (geom,)
        for line in parts:
            coords = np.asarray(line.coords, dtype="float64")
            if len(coords) < 2:
                continue
            xs, ys = inv * (coords[:, 0], coords[:, 1])
            px = np.stack([np.asarray(xs), np.asarray(ys)], axis=1)
            keys = [(round(float(x), 2), round(float(y), 2)) for x, y in px]
            for ka, kb in zip(keys[:-1], keys[1:]):
                if ka == kb:
                    continue
                # Sets to prevent duplicate edges
                adj.setdefault(ka, set()).add(kb)
                adj.setdefault(kb, set()).add(ka)

    return {k: list(v) for k, v in adj.items()}


def on_tile_border(node, size, eps=BORDER_EPS) -> bool:
    x, y = node[0], node[1]
    return x <= eps or y <= eps or x >= size - 1 - eps or y >= size - 1 - eps


def key_nodes(adj, size, eps=BORDER_EPS) -> list:
    """Intersections / endpoints are nodes of degree != 2"""
    return [
        node
        for node, neighbours in adj.items()
        if len(neighbours) != 2 and not on_tile_border(node, size, eps)
    ]


# Adapted from SAM_Road Repo
def draw_points_on_image(size, points, radius=KEYPOINT_RADIUS):
    """
    Draws points on a square image using OpenCV.

    Parameters:
    - size: The size of the square image (width and height) in pixels.
    - points: A list of tuples, where each tuple represents the (x, y) coordinates of a point.
    - radius: The half-width of the square to be drawn for each point, in pixels.
    """
    import cv2

    image = np.zeros((size, size), dtype=np.uint8)

    for point in points:
        x = int(point[0])
        y = int(point[1])
        pt1 = (x - radius, y - radius)
        pt2 = (x + radius, y + radius)

        cv2.rectangle(image, pt1, pt2, 255, -1)

    return image


def keypoint_mask_from_adjacency(adj, size, radius=KEYPOINT_RADIUS, eps=BORDER_EPS):
    return draw_points_on_image(size, key_nodes(adj, size, eps), radius)

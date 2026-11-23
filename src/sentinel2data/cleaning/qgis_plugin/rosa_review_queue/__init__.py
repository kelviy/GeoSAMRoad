"""ROSA review queue: a QGIS plugin that docks the review queue over a zone
project. The plugin imports QGIS only once QGIS loads it, so its client can
be imported and tested without QGIS."""


def classFactory(iface):  # QGIS calls it by this name
    from .plugin import ReviewQueuePlugin

    return ReviewQueuePlugin(iface)

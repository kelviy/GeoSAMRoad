"""Hooks the review queue panel into QGIS: a menu entry, the dock, and
rebinding the panel whenever a project opens or closes."""

import os

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction

from .client import ReviewClient, ServiceError, service_python
from .panel import ReviewPanel
from .zone import Zone

MENU = "&ROSA review queue"


class ReviewQueuePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.client: ReviewClient | None = None
        self.panel: ReviewPanel | None = None
        self.action: QAction | None = None

    def initGui(self):  # QGIS calls it by this name
        try:
            python = service_python(os.path.dirname(__file__))
        except ServiceError as exc:
            self.iface.messageBar().pushCritical("ROSA review queue", str(exc))
            return
        self.client = ReviewClient(python)
        self.panel = ReviewPanel(
            self.iface.mapCanvas(), self.client, self.iface.mainWindow()
        )
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.action = self.panel.toggleViewAction()
        self.action.setText("Review queue panel")
        self.iface.addPluginToMenu(MENU, self.action)
        project = QgsProject.instance()
        project.readProject.connect(self._project_opened)
        project.cleared.connect(self._project_closed)
        self._project_opened()

    def unload(self):
        if self.panel is None:
            return
        project = QgsProject.instance()
        project.readProject.disconnect(self._project_opened)
        project.cleared.disconnect(self._project_closed)
        self.iface.removePluginMenu(MENU, self.action)
        self.iface.removeDockWidget(self.panel)
        self.panel.deleteLater()
        self.panel = None
        if self.client is not None:
            self.client.close()

    def _project_opened(self, *_):
        if self.panel is not None:
            self.panel.bind(Zone.of(QgsProject.instance()))

    def _project_closed(self):
        if self.panel is not None:
            self.panel.bind(None)

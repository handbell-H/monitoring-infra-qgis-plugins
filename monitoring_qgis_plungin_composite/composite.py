from qgis.PyQt.QtWidgets import QAction


class CompositePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.action = None

    def initGui(self):
        self.action = QAction("생활인프라 편리성 종합지수", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("생활인프라 편리성 종합지수", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginMenu("생활인프라 편리성 종합지수", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        if self.dialog is None:
            from .dialog import CompositeDialog
            self.dialog = CompositeDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

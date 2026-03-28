def classFactory(iface):
    from .composite import CompositePlugin
    return CompositePlugin(iface)

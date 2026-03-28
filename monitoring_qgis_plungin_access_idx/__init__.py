def classFactory(iface):
    from .access_idx import AccessIdxPlugin
    return AccessIdxPlugin(iface)

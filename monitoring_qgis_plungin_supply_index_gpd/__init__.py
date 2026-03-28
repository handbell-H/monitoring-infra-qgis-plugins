def classFactory(iface):
    from .supply_index import SupplyIndexPlugin
    return SupplyIndexPlugin(iface)

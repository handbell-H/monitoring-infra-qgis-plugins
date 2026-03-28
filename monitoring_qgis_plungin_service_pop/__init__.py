def classFactory(iface):
    from .service_pop import ServicePopPlugin
    return ServicePopPlugin(iface)

from django.core.cache import cache

from .models import ApiSetting

MAP_CONFIG_CACHE_KEY = 'api:map:cfg'
MAP_CONFIG_CACHE_TIMEOUT = 60


def _fallback_map_config():
    return {
        'map_tile_url': ApiSetting.MAP_PROVIDER_URLS['carto'],
        'map_attribution': ApiSetting.MAP_PROVIDER_ATTRIBUTION['carto'],
        'map_max_zoom': ApiSetting.MAP_DEFAULT_MAX_ZOOM,
        'map_provider': ApiSetting.MapProvider.CARTO,
    }


def _load_map_config():
    try:
        setting = ApiSetting.objects.first()
    except Exception:
        return _fallback_map_config()
    if setting is None:
        return _fallback_map_config()
    return {
        'map_tile_url': setting.map_resolved_tile_url(),
        'map_attribution': setting.map_resolved_attribution(),
        'map_max_zoom': setting.map_max_zoom,
        'map_provider': setting.map_provider,
    }


def map_config(request):
    data = None
    try:
        data = cache.get(MAP_CONFIG_CACHE_KEY)
    except Exception:
        data = None
    if data is None:
        data = _load_map_config()
        try:
            cache.set(MAP_CONFIG_CACHE_KEY, data, MAP_CONFIG_CACHE_TIMEOUT)
        except Exception:
            pass
    return data
FONTS = {
    'default': {'label': 'Default', 'google_name': None},
    'inter': {'label': 'Inter', 'google_name': 'Inter'},
    'poppins': {'label': 'Poppins', 'google_name': 'Poppins'},
    'outfit': {'label': 'Outfit', 'google_name': 'Outfit'},
    'manrope': {'label': 'Manrope', 'google_name': 'Manrope'},
    'space_grotesk': {'label': 'Space Grotesk', 'google_name': 'Space+Grotesk'},
    'exo_2': {'label': 'Exo 2', 'google_name': 'Exo+2'},
    'plus_jakarta': {'label': 'Plus Jakarta Sans', 'google_name': 'Plus+Jakarta+Sans'},
}

CSS_STACKS = {
    'default': None,
    'inter': "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'poppins': "'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'outfit': "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'manrope': "'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'space_grotesk': "'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'exo_2': "'Exo 2', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    'plus_jakarta': "'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
}

WEIGHTS = '400;500;600;700'


def google_fonts_url(font_key):
    """Google Fonts CSS2 URL for a single font, or '' for the default."""
    meta = FONTS.get(font_key or 'default')
    if not meta or not meta.get('google_name'):
        return ''
    return (
        'https://fonts.googleapis.com/css2?family={name}:wght@{weights}&display=swap'
        .format(name=meta['google_name'], weights=WEIGHTS)
    )


def all_fonts_url():
    """Combined URL that loads every selectable font (used for live previews)."""
    names = [m['google_name'] for m in FONTS.values() if m.get('google_name')]
    if not names:
        return ''
    params = '&'.join('family={}:wght@{}'.format(name, WEIGHTS) for name in names)
    return 'https://fonts.googleapis.com/css2?{}&display=swap'.format(params)


def css_stack(font_key):
    """Font-family stack for a font key, or None for the default."""
    return CSS_STACKS.get(font_key or 'default')


def options():
    """Font options for the profile picker: value, label and CSS stack."""
    return [{'value': key, 'label': meta['label'], 'stack': CSS_STACKS.get(key)}
            for key, meta in FONTS.items()]
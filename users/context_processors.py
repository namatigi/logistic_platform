def user_preferences(request):
    theme = 'system'
    notification = 'email'
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        profile = getattr(user, 'profile', None)
        if profile is None:
            try:
                profile = user.profile
            except Exception:
                profile = None
        if profile is not None:
            theme = profile.theme or 'system'
            notification = profile.notification_pref or 'email'
    return {'user_theme': theme, 'user_notification': notification}
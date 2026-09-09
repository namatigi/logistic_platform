import re

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


def _normalize_phone(value):
    return re.sub(r'[\s\-\(\)]', '', value.strip())


class EmailOrPhoneBackend(ModelBackend):
    """Authenticate using email address or phone number (with country code)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = kwargs.get('email') or kwargs.get('phone') or username
        if not identifier:
            return None
        identifier = identifier.strip()
        user = None
        if '@' in identifier:
            user = User.objects.filter(email__iexact=identifier.lower()).first()
        else:
            phone = _normalize_phone(identifier)
            if not phone:
                return None
            user = User.objects.filter(profile__phone=phone).first()
            if user is None:
                stripped = phone.lstrip('+')
                if stripped and len(stripped) >= 8:
                    user = User.objects.filter(profile__phone__endswith=stripped[-9:]).first()
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

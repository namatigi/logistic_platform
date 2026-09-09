from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


class EmailOrPhoneBackend(ModelBackend):
    """Authenticate using email address or phone number (with country code)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = kwargs.get('email') or kwargs.get('phone') or username
        if not identifier:
            return None
        identifier = identifier.strip().lower()
        user = None
        if '@' in identifier:
            user = User.objects.filter(email__iexact=identifier).first()
        else:
            phone = identifier.replace(' ', '').replace('-', '')
            user = (
                User.objects.filter(profile__phone__iexact=phone).first()
                or User.objects.filter(profile__phone__iendswith=phone[-9:]).first()
            )
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

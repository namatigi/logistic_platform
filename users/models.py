from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('The email address must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email, password, **extra_fields)


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        ADMINISTRATOR = 'administrator', 'Administrator'
        AGENT = 'agent', 'Agent'
        USER = 'user', 'User'

    username = None
    email = models.EmailField(_('email address'), unique=True)
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.USER,
        help_text='Role in the HYPAX platform',
    )

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def avatar_picture(self):
        try:
            profile = self.profile
        except Profile.DoesNotExist:
            return ''
        return profile.profile_picture.url if profile.profile_picture else ''

    def avatar_initials(self):
        local = (self.email or '?').split('@', 1)[0]
        words = [w for w in local.replace('_', ' ').replace('.', ' ').replace('-', ' ').split() if w]
        return ''.join(w[0].upper() for w in words[:2]) or '?'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.sync_role_groups()

    def sync_role_groups(self):
        from django.contrib.auth.models import Group

        groups = {
            self.Role.ADMINISTRATOR: 'Administrator',
            self.Role.AGENT: 'Agents',
            self.Role.USER: 'Users',
        }
        target = groups.get(self.role)
        if not target:
            return
        member_of = set(self.groups.values_list('name', flat=True))
        desired = set(groups.values())
        for name in desired - {target}:
            if name in member_of:
                group = Group.objects.filter(name=name).first()
                if group:
                    self.groups.remove(group)
        group, _ = Group.objects.get_or_create(name=target)
        self.groups.add(group)


class Profile(models.Model):
    class Theme(models.TextChoices):
        SYSTEM = 'system', 'System'
        LIGHT = 'light', 'Light'
        DARK = 'dark', 'Dark'

    class Notifications(models.TextChoices):
        EMAIL = 'email', 'By Email'
        HYPAX = 'hypax', 'In HYPAX'

    class Fonts(models.TextChoices):
        DEFAULT = 'default', 'Default'
        INTER = 'inter', 'Inter'
        POPPINS = 'poppins', 'Poppins'
        OUTFIT = 'outfit', 'Outfit'
        MANROPE = 'manrope', 'Manrope'
        SPACE_GROTESK = 'space_grotesk', 'Space Grotesk'
        EXO_2 = 'exo_2', 'Exo 2'
        PLUS_JAKARTA = 'plus_jakarta', 'Plus Jakarta Sans'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    bio = models.TextField(blank=True, default='', help_text='Short personal or company bio')
    profile_picture = models.ImageField(upload_to='profiles/', null=True, blank=True)
    phone = models.CharField(max_length=50, blank=True, default='')
    theme = models.CharField(
        max_length=20, choices=Theme.choices, default=Theme.SYSTEM,
        help_text='UI theme: follow the system, or force light/dark.',
    )
    notification_pref = models.CharField(
        max_length=20, choices=Notifications.choices, default=Notifications.EMAIL,
        help_text='How the account owner wants to receive notifications.',
    )
    font_pref = models.CharField(
        max_length=30, choices=Fonts.choices, default=Fonts.DEFAULT,
        help_text='Web font applied to the whole platform for this account.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Profile for {self.user.email}'


class Address(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='addresses',
    )
    label = models.CharField(max_length=100, blank=True, default='', help_text='e.g. Home, Office, Warehouse')
    street = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    postal_code = models.CharField(max_length=20, blank=True, default='')
    country = models.CharField(max_length=100, blank=True, default='')
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        parts = [self.street, self.city, self.country]
        location = ', '.join(p for p in parts if p)
        return f'{self.label or "Address"} ({self.user.email})' + (f' — {location}' if location else '')

    class Meta:
        verbose_name_plural = 'addresses'
        ordering = ['-is_primary', 'created_at']
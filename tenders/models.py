import random
import string

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.geos import Point
from django.db import models

from .towns import TOWN_CHOICES


def generate_transporter_alias():
    """Random alphanumeric alias (uppercase letters + digits) unique per order."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        alias = ''.join(random.choice(alphabet) for _ in range(8))
        if not (any(c.isalpha() for c in alias) and any(c.isdigit() for c in alias)):
            continue
        if not Order.objects.filter(transporter_alias=alias).exists():
            return alias


class Tender(models.Model):
    class CargoType(models.TextChoices):
        CONTAINER_20 = 'container_20', 'Container 20ft'
        CONTAINER_40 = 'container_40', 'Container 40ft'
        BREAK_BULK = 'break_bulk', 'Break Bulk'
        BULK = 'bulk', 'Bulk'
        DRY_VAN = 'dry_van', 'Dry Van'
        REEFER = 'reefer', 'Reefer'

    class TruckType(models.TextChoices):
        TRAILER = 'trailer', 'Trailer'
        FLATBED = 'flatbed', 'Flatbed'
        TRUCK = 'truck', 'Truck'
        SIDELOADER = 'sideloader', 'Sideloader'
        LOWBED = 'lowbed', 'Lowbed'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tenders')
    route_loading = models.CharField(max_length=255, choices=TOWN_CHOICES)
    route_delivery = models.CharField(max_length=255, choices=TOWN_CHOICES)
    customer = models.CharField(max_length=200)
    cargo_type = models.CharField(max_length=50, choices=CargoType.choices)
    truck_type = models.CharField(max_length=50, choices=TruckType.choices)
    weight = models.FloatField()
    number_of_trucks = models.PositiveIntegerField()
    distance_km = models.FloatField()
    cargo_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    response_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    external_id = models.BigIntegerField(null=True, blank=True, help_text='ID returned by the external system')
    cargo_reference = models.CharField(max_length=200, blank=True, default='', help_text='Reference returned by the external system (e.g. CAR00014)')
    external_status = models.CharField(max_length=50, blank=True, default='', help_text='Status returned by the external system')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.customer} - {self.route_loading} to {self.route_delivery}'

    class Meta:
        ordering = ['-created_at']


class Town(models.Model):
    name = models.CharField(max_length=255, unique=True)
    country = models.CharField(max_length=100)
    point = gis_models.PointField(srid=4326, help_text='Geographic location (longitude, latitude)')

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name}, {self.country}'

    @property
    def lat(self):
        return self.point.y if self.point else 0

    @property
    def lng(self):
        return self.point.x if self.point else 0

    def save(self, *args, **kwargs):
        if self.point is not None and not isinstance(self.point, Point):
            self.point = Point(*self.point)
        super().save(*args, **kwargs)


class ApiSetting(models.Model):
    class AuthType(models.TextChoices):
        NONE = 'none', 'No Auth'
        BEARER = 'bearer', 'Bearer Token'
        BASIC = 'basic', 'Basic Auth'

    base_url = models.CharField(max_length=500, help_text='e.g. https://example.com')
    auth_type = models.CharField(max_length=20, choices=AuthType.choices, default=AuthType.BEARER)
    api_token = models.CharField(max_length=500, blank=True, help_text='Token used for Bearer authentication')
    username = models.CharField(max_length=200, blank=True)
    password = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        setting = cls.objects.first()
        if setting is None:
            setting = cls.objects.create()
        return setting

    def endpoint_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/tenders'

    def order_confirmation_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/order-confirmation'

    def partial_order_confirmation_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/partial-order-confirmation'

    def order_invoice_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/order-invoice'

    def __str__(self):
        return f'Shared API settings ({self.base_url or "not configured"})'


class Order(models.Model):
    order_id = models.PositiveBigIntegerField(unique=True)
    order_name = models.CharField(max_length=255, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    company_id = models.PositiveBigIntegerField(null=True, blank=True)
    company_name = models.CharField(max_length=255, blank=True, default='')
    transporter_alias = models.CharField(max_length=40, blank=True, default='', help_text='Random anonymous alias shown instead of the transporter name')
    date_order = models.DateTimeField(null=True, blank=True)
    amount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    customer = models.CharField(max_length=255, blank=True, default='')
    currency = models.CharField(max_length=10, blank=True, default='')
    cargo_reference = models.CharField(max_length=255, blank=True, default='', db_index=True)
    cargo_id = models.PositiveBigIntegerField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='orders',
    )
    tender = models.ForeignKey(Tender, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders')
    raw_payload = models.JSONField(default=dict)
    award_response = models.JSONField(null=True, blank=True, default=dict)
    awarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.order_name} ({self.customer})'

    @property
    def award_response_data(self):
        value = self.award_response
        return value if isinstance(value, dict) else {}

    @property
    def award_data(self):
        data = self.award_response_data.get('data')
        return data if isinstance(data, dict) else self.award_response_data

    @property
    def remaining_line_ids(self):
        ids = self.award_data.get('remaining_line_ids')
        return list(ids) if isinstance(ids, (list, tuple)) else []

    @property
    def removed_line_ids(self):
        ids = self.award_data.get('removed_line_ids')
        return list(ids) if isinstance(ids, (list, tuple)) else []

    @property
    def fully_confirmed(self):
        data = self.award_data
        return bool(data) and not (data.get('removed_line_ids') or data.get('remaining_line_ids'))

    @property
    def partially_confirmed(self):
        data = self.award_data
        return bool(data.get('removed_line_ids')) or bool(data.get('remaining_line_ids'))

    @property
    def awarded_amount(self):
        total = self.lines.filter(awarded=True).aggregate(
            total=models.Sum('price_total')
        )['total']
        return total or 0

    @property
    def awarded_lines_count(self):
        return self.lines.filter(awarded=True).count()

    class Meta:
        ordering = ['-date_order', '-created_at']
        indexes = [
            models.Index(fields=['-date_order', '-created_at'], name='order_dates_idx'),
        ]


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='lines')
    line_id = models.PositiveBigIntegerField()
    product_id = models.PositiveBigIntegerField(null=True, blank=True)
    product_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    price_unit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    price_subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    price_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    awarded = models.BooleanField(default=False, help_text='Marked when this order line is confirmed/awarded')

    class Meta:
        indexes = [
            models.Index(fields=['order', 'awarded'], name='orderline_order_awarded_idx'),
            models.Index(fields=['awarded'], name='orderline_awarded_idx'),
        ]

    def __str__(self):
        return f'{self.product_name} x {self.quantity}'


class Invoice(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'

    number = models.CharField(max_length=50, unique=True)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='invoice')
    transporter = models.ForeignKey(
        'Transporter', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invoices',
        help_text='Transport company this invoice is issued to',
    )
    amount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=10, blank=True, default='')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.number} ({self.order_id})'

    class Meta:
        ordering = ['-created_at']


class Transporter(models.Model):
    company_id = models.PositiveBigIntegerField(null=True, blank=True, help_text='ID of the transport company in the external system')
    company_name = models.CharField(max_length=255, blank=True, default='', help_text='Name of the transport company')
    alias = models.CharField(max_length=40, blank=True, default='', help_text='Anonymous alias shown instead of the transporter name')
    agents = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name='linked_transporters',
        help_text='Agents that can see this transporter\'s tenders, orders and invoices',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.company_name or self.alias or f'Transporter #{self.company_id or self.pk}'

    class Meta:
        ordering = ['company_name', 'alias']
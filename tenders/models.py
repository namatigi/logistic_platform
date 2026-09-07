from django.conf import settings
from django.db import models

from .towns import TOWN_CHOICES


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


class ApiSetting(models.Model):
    class AuthType(models.TextChoices):
        NONE = 'none', 'No Auth'
        BEARER = 'bearer', 'Bearer Token'
        BASIC = 'basic', 'Basic Auth'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_setting')
    base_url = models.CharField(max_length=500, help_text='e.g. https://example.com')
    auth_type = models.CharField(max_length=20, choices=AuthType.choices, default=AuthType.BEARER)
    api_token = models.CharField(max_length=500, blank=True, help_text='Token used for Bearer authentication')
    username = models.CharField(max_length=200, blank=True)
    password = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def endpoint_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/tenders'

    def order_confirmation_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/order-confirmation'

    def partial_order_confirmation_url(self):
        base = self.base_url.rstrip('/')
        return f'{base}/api/v1/partial-order-confirmation'

    def __str__(self):
        return f'API settings for {self.user.email}'


class Order(models.Model):
    order_id = models.PositiveBigIntegerField(unique=True)
    order_name = models.CharField(max_length=255, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    company_id = models.PositiveBigIntegerField(null=True, blank=True)
    company_name = models.CharField(max_length=255, blank=True, default='')
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

    class Meta:
        ordering = ['-date_order', '-created_at']


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

    def __str__(self):
        return f'{self.product_name} x {self.quantity}'
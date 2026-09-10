import random
import string

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.geos import Point
from django.db import models
from django.utils.text import slugify

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
    payment_terms = models.ForeignKey(
        'PaymentTerm', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='tenders', help_text='Payment terms chosen by the posting user for this tender.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.customer} - {self.route_loading} to {self.route_delivery}'

    class Meta:
        ordering = ['-created_at']


class PaymentTerm(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payment_terms',
        help_text='User who created this payment terms.',
    )
    name = models.CharField(max_length=100, help_text='Short label, e.g. Net 30 or Payment on confirmation.')
    description = models.TextField(blank=True, default='', help_text='Full details of the payment terms.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['-created_at']


class PaymentTermItem(models.Model):
    payment_term = models.ForeignKey(
        PaymentTerm, on_delete=models.CASCADE, related_name='items',
        help_text='Payment term this individual term belongs to.',
    )
    text = models.CharField(max_length=300, help_text='A single term detail, e.g. 50% advance on confirmation.')
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text

    class Meta:
        ordering = ['sort_order', 'created_at']


class EscrowAccount(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        CLOSED = 'closed', 'Closed'

    tender = models.OneToOneField(Tender, on_delete=models.CASCADE, related_name='escrow_account')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='escrow_accounts',
        help_text='Customer who posted the tender and deposits into this account.',
    )
    virtual_account = models.CharField(max_length=120, blank=True, default='', help_text='Escrow account number shown to the customer.')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text='Expected amount to be deposited.')
    deposited_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text='Amount actually deposited (reported by the payment gateway).')
    payment_terms = models.ForeignKey(
        PaymentTerm, null=True, blank=True, on_delete=models.SET_NULL, related_name='escrow_accounts',
    )
    transporters = models.ManyToManyField(
        'Transporter', blank=True, related_name='escrow_accounts',
        help_text='Transport companies related to this escrow account. The same cargo reference can have multiple transporters.',
    )
    invoices = models.ManyToManyField(
        'Invoice', blank=True, related_name='escrow_accounts',
        help_text='Invoices issued against this cargo reference. A cargo reference can have more than one invoice.',
    )
    bank = models.CharField(max_length=60, blank=True, default='Selcom')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.virtual_account or f'Escrow for tender #{self.tender_id}'

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

    class MediaStorageOption(models.TextChoices):
        LOCAL = 'local', 'Option A - Server volume'
        S3 = 's3', 'Option B - S3 object storage'

    media_storage = models.CharField(
        max_length=20,
        choices=MediaStorageOption.choices,
        default=MediaStorageOption.LOCAL,
        help_text='Where uploaded files (profile pictures, etc.) are stored. Select on Configuration > Files.',
    )

    base_url = models.CharField(max_length=500, help_text='e.g. https://example.com')
    auth_type = models.CharField(max_length=20, choices=AuthType.choices, default=AuthType.BEARER)
    api_token = models.CharField(max_length=500, blank=True, help_text='Token used for Bearer authentication')
    username = models.CharField(max_length=200, blank=True)
    password = models.CharField(max_length=200, blank=True)
    selcom_enabled = models.BooleanField(
        default=False,
        help_text='Enable Selcom payment gateway for invoice payments.',
    )
    selcom_sandbox = models.BooleanField(
        default=True,
        help_text='Use Selcom sandbox (apigwdev) endpoints. Disable for production.',
    )
    selcom_base_url = models.CharField(
        max_length=500, blank=True,
        help_text='Optional Selcom API root. Defaults to sandbox or production APIGW.',
    )
    selcom_client_id = models.CharField(max_length=200, blank=True)
    selcom_client_secret = models.CharField(max_length=200, blank=True)
    selcom_sales_channel = models.CharField(
        max_length=100, blank=True,
        help_text='Sales channel / vendor name, e.g. PURCHASE.',
    )
    selcom_currency = models.CharField(max_length=10, default='TZS', blank=True)
    selcom_payment_methods = models.CharField(
        max_length=500, blank=True,
        help_text='Comma-separated wallets/banks, e.g. MPESA,TIGOPESA,AIRTELMONEY,HALOPESA,CRDB,NMB',
    )
    selcom_webhook_secret = models.CharField(
        max_length=500, blank=True,
        help_text='Shared secret used to verify Selcom payment callback signatures.',
    )
    selcom_paylink_base = models.CharField(
        max_length=500, blank=True,
        help_text='Optional hosted checkout root. Defaults to the Selcom API root.',
    )
    email_host = models.CharField(max_length=255, blank=True, default='imap.gmail.com')
    email_port = models.IntegerField(default=993)
    email_use_ssl = models.BooleanField(default=True)
    email_username = models.CharField(max_length=255, blank=True)
    email_password = models.CharField(max_length=255, blank=True)
    smtp_host = models.CharField(max_length=255, blank=True, default='')
    smtp_port = models.IntegerField(default=587)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_use_ssl = models.BooleanField(default=False)
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password = models.CharField(max_length=255, blank=True)
    email_from = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    SELCOM_SANDBOX_BASE = 'https://apigwdev.selcommobile.com/v1'
    SELCOM_PRODUCTION_BASE = 'https://apigw.selcommobile.com/v1'
    SELCOM_DEFAULT_METHODS = 'MPESA,TIGOPESA,AIRTELMONEY,HALOPESA'

    DEFAULT_TENDERS_PATH = '/api/v1/tenders'
    DEFAULT_ORDER_CONFIRMATION_PATH = '/api/v1/order-confirmation'
    DEFAULT_PARTIAL_ORDER_CONFIRMATION_PATH = '/api/v1/partial-order-confirmation'
    DEFAULT_ORDER_INVOICE_PATH = '/api/v1/order-invoice'

    tenders_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path tenders are posted to. Default: /api/v1/tenders',
    )
    order_confirmation_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path order confirmations are posted to. Default: /api/v1/order-confirmation',
    )
    partial_order_confirmation_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path partial order confirmations are posted to. Default: /api/v1/partial-order-confirmation',
    )
    order_invoice_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path order invoices are posted to. Default: /api/v1/order-invoice',
    )

    @classmethod
    def get(cls):
        setting = cls.objects.first()
        if setting is None:
            setting = cls.objects.create()
        return setting

    def selcom_api_base(self):
        if self.selcom_base_url.strip():
            return self.selcom_base_url.rstrip('/')
        if self.selcom_sandbox:
            return self.SELCOM_SANDBOX_BASE
        return self.SELCOM_PRODUCTION_BASE

    def selcom_api_url(self, path):
        base = self.selcom_api_base()
        path = path.lstrip('/')
        return f'{base}/{path}'

    def selcom_payment_link(self, order_token):
        base = self.selcom_paylink_base.strip() or self.selcom_api_base()
        return f'{base.rstrip("/")}/checkout/paylink/{order_token}'

    def selcom_methods_list(self):
        methods = [m.strip() for m in self.selcom_payment_methods.split(',') if m.strip()]
        return methods or [m.strip() for m in self.SELCOM_DEFAULT_METHODS.split(',')]

    def _path_url(self, path, default):
        base = self.base_url.rstrip('/')
        path = (path or default).strip().lstrip('/')
        return f'{base}/{path}'

    def endpoint_url(self):
        return self._path_url(self.tenders_path, self.DEFAULT_TENDERS_PATH)

    def order_confirmation_url(self):
        return self._path_url(self.order_confirmation_path, self.DEFAULT_ORDER_CONFIRMATION_PATH)

    def partial_order_confirmation_url(self):
        return self._path_url(self.partial_order_confirmation_path, self.DEFAULT_PARTIAL_ORDER_CONFIRMATION_PATH)

    def order_invoice_url(self):
        return self._path_url(self.order_invoice_path, self.DEFAULT_ORDER_INVOICE_PATH)

    def __str__(self):
        return f'Shared API settings ({self.base_url or "not configured"})'


class OdooCompany(models.Model):
    class AuthType(models.TextChoices):
        NONE = 'none', 'No Auth'
        BEARER = 'bearer', 'Bearer Token'
        BASIC = 'basic', 'Basic Auth'

    name = models.CharField(max_length=200, help_text='Company / Odoo instance label, e.g. ACSC Ltd.')
    slug = models.SlugField(
        max_length=200, unique=True, blank=True,
        help_text='URL-safe identifier used in the per-instance webhook URL.',
    )
    base_url = models.CharField(max_length=500, help_text='e.g. https://odoo.acsc.com')
    auth_type = models.CharField(max_length=20, choices=AuthType.choices, default=AuthType.BEARER)
    api_token = models.CharField(max_length=500, blank=True, help_text='Token used for Bearer authentication')
    username = models.CharField(max_length=200, blank=True)
    password = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive companies do not appear on the webhook URL selectable list.',
    )
    tenders_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path tenders are posted to. Default: /api/v1/tenders',
    )
    order_confirmation_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path order confirmations are posted to. Default: /api/v1/order-confirmation',
    )
    partial_order_confirmation_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path partial order confirmations are posted to. Default: /api/v1/partial-order-confirmation',
    )
    order_invoice_path = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Path order invoices are posted to. Default: /api/v1/order-invoice',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Odoo company'
        verbose_name_plural = 'Odoo companies'

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or 'company'
            slug, n = base, 2
            while OdooCompany.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def _path_url(self, path, default):
        base = self.base_url.rstrip('/')
        path = (path or default).strip().lstrip('/')
        return f'{base}/{path}'

    def endpoint_url(self):
        return self._path_url(self.tenders_path, ApiSetting.DEFAULT_TENDERS_PATH)

    def order_confirmation_url(self):
        return self._path_url(self.order_confirmation_path, ApiSetting.DEFAULT_ORDER_CONFIRMATION_PATH)

    def partial_order_confirmation_url(self):
        return self._path_url(self.partial_order_confirmation_path, ApiSetting.DEFAULT_PARTIAL_ORDER_CONFIRMATION_PATH)

    def order_invoice_url(self):
        return self._path_url(self.order_invoice_path, ApiSetting.DEFAULT_ORDER_INVOICE_PATH)

    def __str__(self):
        return self.name


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
    odoo_company = models.ForeignKey(
        OdooCompany, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='orders', help_text='Odoo company/instance this order came from. Award and invoice confirmations are sent back to it.',
    )
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
    deposited_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text='Amount actually collected by the payment gateway for this invoice.')
    currency = models.CharField(max_length=10, blank=True, default='')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    selcom_reference = models.CharField(max_length=120, blank=True, default='', help_text='Vendor reference used when creating the Selcom checkout order')
    selcom_order_token = models.CharField(max_length=255, blank=True, default='', help_text='Order token returned by Selcom when the checkout order was created')
    selcom_pay_link = models.CharField(max_length=600, blank=True, default='', help_text='Hosted checkout URL for the invoice payment')
    selcom_status = models.CharField(max_length=40, blank=True, default='', help_text='Latest payment status reported by Selcom')
    selcom_updated_at = models.DateTimeField(null=True, blank=True, help_text='When the Selcom payment status was last checked')
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


class TruckModel(models.Model):
    class FuelType(models.TextChoices):
        DIESEL = 'diesel', 'Diesel'
        PETROL = 'petrol', 'Petrol / Gasoline'
        ELECTRIC = 'electric', 'Electric'
        HYBRID = 'hybrid', 'Hybrid'
        CNG = 'cng', 'CNG / LPG'

    class Transmission(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        AUTOMATIC = 'automatic', 'Automatic'
        AMT = 'amt', 'Automated Manual (AMT)'

    class DriveType(models.TextChoices):
        R4X2 = '4x2', '4x2'
        R4X4 = '4x4', '4x4'
        R6X2 = '6x2', '6x2'
        R6X4 = '6x4', '6x4'
        R8X4 = '8x4', '8x4'
        AWD = 'awd', 'AWD'

    name = models.CharField(max_length=150)
    manufacturer = models.CharField(max_length=150, blank=True, default='')
    vehicle_type = models.CharField(max_length=50, choices=Tender.TruckType.choices, blank=True, default='')
    model_year = models.PositiveIntegerField(null=True, blank=True)
    volume_capacity = models.FloatField(null=True, blank=True, help_text='Volume capacity for tanks (cubic metres)')
    tonnage_capacity = models.FloatField(null=True, blank=True, help_text='Tonnage capacity (tonnes)')
    number_of_axles = models.PositiveIntegerField(null=True, blank=True)
    fuel_type = models.CharField(max_length=20, choices=FuelType.choices, blank=True, default='')
    transmission = models.CharField(max_length=20, choices=Transmission.choices, blank=True, default='')
    drive_type = models.CharField(max_length=10, choices=DriveType.choices, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return ' '.join(filter(None, [self.manufacturer, self.name])) or f'Model #{self.pk}'

    class Meta:
        ordering = ['manufacturer', 'name']


class Truck(models.Model):
    transporter = models.ForeignKey(Transporter, on_delete=models.CASCADE, related_name='trucks')
    truck_model = models.ForeignKey(
        TruckModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='trucks',
        help_text='Optionally links this truck to a catalog model',
    )
    model = models.CharField(max_length=120, blank=True, default='')
    license_plate = models.CharField(max_length=40, blank=True, default='')
    tags = models.CharField(max_length=200, blank=True, default='')
    chassis_number = models.CharField(max_length=120, blank=True, default='')
    model_year = models.PositiveIntegerField(null=True, blank=True)
    tonnage_capacity = models.FloatField(null=True, blank=True, help_text='Tonnage capacity (tonnes)')
    number_of_axles = models.PositiveIntegerField(null=True, blank=True)
    volume_capacity = models.FloatField(null=True, blank=True, help_text='Volume capacity for tanks (cubic metres)')
    truck_type = models.CharField(max_length=50, choices=Tender.TruckType.choices, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.license_plate or self.model or f'Truck #{self.pk}'

    class Meta:
        ordering = ['-created_at']
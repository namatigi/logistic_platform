from django.conf import settings
from django.db import models


class Company(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='companies')
    name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=100, blank=True)
    tin = models.CharField(max_length=100, blank=True, help_text='Tax Identification Number (TIN)')
    vat = models.CharField(max_length=100, blank=True, help_text='Value Added Tax (VAT) number')
    contact_person = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    odoo_company = models.ForeignKey(
        'tenders.OdooCompany',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='registered_companies',
        help_text='The Odoo company/instance this transporter posts its tenders to. Tenders are submitted to its base URL when linked.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = 'companies'
        ordering = ['name']
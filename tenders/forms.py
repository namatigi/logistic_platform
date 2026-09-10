from django import forms

from .models import ApiSetting, OdooCompany, PaymentTerm, Tender


class TenderForm(forms.ModelForm):
    class Meta:
        model = Tender
        fields = (
            'route_loading',
            'route_delivery',
            'customer',
            'cargo_type',
            'truck_type',
            'weight',
            'number_of_trucks',
            'distance_km',
            'cargo_date',
            'payment_terms',
        )
        widgets = {
            'cargo_date': forms.DateInput(attrs={'type': 'date'}),
            'weight': forms.NumberInput(attrs={'step': '0.1'}),
            'distance_km': forms.NumberInput(attrs={'step': '0.1'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields['payment_terms'].queryset = PaymentTerm.objects.filter(user=user)


class PaymentTermForm(forms.ModelForm):
    class Meta:
        model = PaymentTerm
        fields = ('name', 'description', 'is_active')
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }


class ApiSettingForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = (
            'base_url', 'auth_type', 'api_token', 'username', 'password',
            'tenders_path', 'order_confirmation_path', 'partial_order_confirmation_path',
            'order_invoice_path',
            'selcom_enabled', 'selcom_sandbox', 'selcom_base_url',
            'selcom_client_id', 'selcom_client_secret', 'selcom_sales_channel',
            'selcom_currency', 'selcom_payment_methods', 'selcom_webhook_secret',
            'selcom_paylink_base',
        )


class OdooCompanyForm(forms.ModelForm):
    class Meta:
        model = OdooCompany
        fields = (
            'name', 'slug', 'base_url', 'auth_type', 'api_token', 'username', 'password',
            'tenders_path', 'order_confirmation_path', 'partial_order_confirmation_path',
            'order_invoice_path', 'is_active',
        )
        help_texts = {
            'slug': 'Optional. Leave blank to auto-generate from the name.',
            'is_active': 'Inactive companies stop accepting webhooks and are hidden from new tenders.',
            'tenders_path': 'Leave blank to use /api/v1/tenders.',
            'order_confirmation_path': 'Leave blank to use /api/v1/order-confirmation.',
            'partial_order_confirmation_path': 'Leave blank to use /api/v1/partial-order-confirmation.',
            'order_invoice_path': 'Leave blank to use /api/v1/order-invoice.',
        }
        widgets = {
            'password': forms.PasswordInput(render_value=True),
        }


class SharedOdooConfigForm(ApiSettingForm):
    """ApiSettingForm without the four API path fields (edited on Configuration > API Settings)."""

    class Meta(ApiSettingForm.Meta):
        fields = ('base_url', 'auth_type', 'api_token', 'username', 'password')


class OdooCompanyConfigForm(OdooCompanyForm):
    """OdooCompanyForm without the four API path fields (edited on Configuration > API Settings)."""

    class Meta(OdooCompanyForm.Meta):
        fields = (
            'name', 'slug', 'base_url', 'auth_type', 'api_token', 'username', 'password', 'is_active',
        )


class SelcomConfigForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = (
            'selcom_enabled', 'selcom_sandbox', 'selcom_base_url',
            'selcom_client_id', 'selcom_client_secret', 'selcom_sales_channel',
            'selcom_currency', 'selcom_payment_methods', 'selcom_webhook_secret',
            'selcom_paylink_base',
        )


class IncomingEmailConfigForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = (
            'email_host', 'email_port', 'email_use_ssl',
            'email_username', 'email_password',
        )
        widgets = {
            'email_port': forms.NumberInput(),
            'email_password': forms.PasswordInput(render_value=True),
        }


class OutgoingEmailConfigForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = (
            'smtp_host', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl',
            'smtp_username', 'smtp_password', 'email_from',
        )
        widgets = {
            'smtp_port': forms.NumberInput(),
            'smtp_use_tls': forms.CheckboxInput(),
            'smtp_use_ssl': forms.CheckboxInput(),
            'smtp_password': forms.PasswordInput(render_value=True),
        }


class MediaConfigForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = ('media_storage',)
from django import forms

from .models import ApiSetting, Tender


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
        )
        widgets = {
            'cargo_date': forms.DateInput(attrs={'type': 'date'}),
            'weight': forms.NumberInput(attrs={'step': '0.1'}),
            'distance_km': forms.NumberInput(attrs={'step': '0.1'}),
        }


class ApiSettingForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = (
            'base_url', 'auth_type', 'api_token', 'username', 'password',
            'selcom_enabled', 'selcom_sandbox', 'selcom_base_url',
            'selcom_client_id', 'selcom_client_secret', 'selcom_sales_channel',
            'selcom_currency', 'selcom_payment_methods', 'selcom_webhook_secret',
            'selcom_paylink_base',
        )


class OdooConfigForm(forms.ModelForm):
    class Meta:
        model = ApiSetting
        fields = ('base_url', 'auth_type', 'api_token', 'username', 'password')


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
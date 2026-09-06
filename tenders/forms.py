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
        fields = ('base_url', 'auth_type', 'api_token', 'username', 'password')
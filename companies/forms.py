from django import forms

from .models import Company


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ('name', 'registration_number', 'contact_person', 'phone', 'email', 'address', 'city', 'country')
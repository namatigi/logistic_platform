from django import forms
from django.core.exceptions import ValidationError

from .models import Address, CustomUser, Profile


class SignUpForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm Password')

    class Meta:
        model = CustomUser
        fields = ('email',)

    def clean_password2(self):
        p1 = self.cleaned_data.get('password1')
        p2 = self.cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


class ProfileForm(forms.ModelForm):
    email = forms.EmailField(required=True, label='Email address')

    class Meta:
        model = Profile
        fields = ('bio', 'profile_picture', 'phone')


class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = ('label', 'street', 'city', 'postal_code', 'country', 'is_primary')
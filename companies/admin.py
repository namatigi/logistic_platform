from django.contrib import admin

from .models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'registration_number', 'city', 'country')
    list_select_related = ('user',)
    search_fields = ('name', 'registration_number')
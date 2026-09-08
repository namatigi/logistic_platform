from django.contrib import admin

from .models import ApiSetting, Order, OrderLine, Tender, Town


@admin.register(Town)
class TownAdmin(admin.ModelAdmin):
    list_display = ('name', 'country', 'lat', 'lng')
    list_filter = ('country',)
    search_fields = ('name', 'country')


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    list_display = ('customer', 'route_loading', 'route_delivery', 'cargo_date', 'cargo_reference', 'status', 'response_code')
    list_filter = ('status', 'cargo_type', 'truck_type')
    search_fields = ('customer', 'route_loading', 'route_delivery', 'cargo_reference')


@admin.register(ApiSetting)
class ApiSettingAdmin(admin.ModelAdmin):
    list_display = ('base_url', 'auth_type', 'updated_at')
    search_fields = ('base_url',)


class OrderLineInline(admin.TabularInline):
    model = OrderLine
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('order_name', 'order_id', 'customer', 'state', 'currency', 'amount_total', 'cargo_reference', 'date_order')
    list_filter = ('state', 'currency')
    search_fields = ('order_name', 'order_id', 'customer', 'cargo_reference')
    inlines = (OrderLineInline,)
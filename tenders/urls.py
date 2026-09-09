from django.urls import path

from . import views

app_name = 'tenders'

urlpatterns = [
    path('', views.Dashboard.as_view(), name='dashboard'),
    path('tenders/', views.TenderList.as_view(), name='list'),
    path('tenders/new/', views.TenderCreate.as_view(), name='create'),
    path('tenders/route/', views.route_map, name='route_map'),
    path('tenders/tracker/', views.tracker, name='tracker'),
    path('orders/', views.OrderList.as_view(), name='order_list'),
    path('orders/<int:pk>/', views.OrderDetail.as_view(), name='order_detail'),
    path('orders/<int:pk>/award/', views.award_order, name='order_award'),
    path('orders/<int:pk>/pay/', views.make_payment, name='order_pay'),
    path('invoices/', views.invoice_list, name='invoices'),
    path('api/invoices/', views.api_invoices, name='api_invoices'),
    path('api/invoices/<int:pk>/paid/', views.api_invoice_paid, name='api_invoice_paid'),
    path('api/invoices/<int:pk>/selcom/initiate/', views.api_invoice_selcom_initiate, name='api_invoice_selcom_initiate'),
    path('api/invoices/<int:pk>/selcom/status/', views.api_invoice_selcom_status, name='api_invoice_selcom_status'),
    path('settings/', views.ApiSettingUpdate.as_view(), name='api_settings'),
    path('configuration/odoo/', views.config_odoo, name='config_odoo'),
    path('configuration/selcom/', views.config_selcom, name='config_selcom'),
    path('configuration/email/', views.config_email, name='config_email'),
    path('webhook/orders/', views.webhook_orders, name='webhook_orders'),
    path('webhook/selcom/', views.webhook_selcom, name='webhook_selcom'),
    path('api/dashboard/', views.api_dashboard, name='api_dashboard'),
    path('api/tenders/', views.api_tender_list, name='api_tender_list'),
    path('api/tenders/new/', views.api_tender_create, name='api_tender_create'),
    path('api/form-meta/', views.api_form_meta, name='api_form_meta'),
    path('api/orders/', views.api_order_list, name='api_order_list'),
    path('api/orders/<int:pk>/', views.api_order_detail, name='api_order_detail'),
    path('api/orders/<int:pk>/award/', views.api_order_award, name='api_order_award'),
    path('api/orders/<int:pk>/pay/', views.api_order_pay, name='api_order_pay'),
    path('api/settings/', views.api_settings, name='api_settings_json'),
    path('api/towns/', views.api_towns, name='api_towns'),
    path('api/town-route/', views.api_town_route, name='api_town_route'),
    path('api/tracker/', views.api_tracker, name='api_tracker'),
]
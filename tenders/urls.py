from django.urls import path

from . import views

app_name = 'tenders'

urlpatterns = [
    path('', views.Dashboard.as_view(), name='dashboard'),
    path('tenders/', views.TenderList.as_view(), name='list'),
    path('tenders/new/', views.TenderCreate.as_view(), name='create'),
    path('orders/', views.OrderList.as_view(), name='order_list'),
    path('orders/<int:pk>/', views.OrderDetail.as_view(), name='order_detail'),
    path('orders/<int:pk>/award/', views.award_order, name='order_award'),
    path('orders/<int:pk>/pay/', views.make_payment, name='order_pay'),
    path('settings/', views.ApiSettingUpdate.as_view(), name='api_settings'),
    path('webhook/orders/', views.webhook_orders, name='webhook_orders'),
    path('api/dashboard/', views.api_dashboard, name='api_dashboard'),
    path('api/tenders/', views.api_tender_list, name='api_tender_list'),
    path('api/tenders/new/', views.api_tender_create, name='api_tender_create'),
    path('api/form-meta/', views.api_form_meta, name='api_form_meta'),
    path('api/orders/', views.api_order_list, name='api_order_list'),
    path('api/orders/<int:pk>/', views.api_order_detail, name='api_order_detail'),
    path('api/orders/<int:pk>/award/', views.api_order_award, name='api_order_award'),
    path('api/orders/<int:pk>/pay/', views.api_order_pay, name='api_order_pay'),
    path('api/settings/', views.api_settings, name='api_settings'),
]
from django.urls import path

from . import views

app_name = 'tenders'

urlpatterns = [
    path('', views.Dashboard.as_view(), name='dashboard'),
    path('tenders/', views.TenderList.as_view(), name='list'),
    path('tenders/new/', views.TenderCreate.as_view(), name='create'),
    path('orders/', views.OrderList.as_view(), name='order_list'),
    path('orders/<int:pk>/', views.OrderDetail.as_view(), name='order_detail'),
    path('settings/', views.ApiSettingUpdate.as_view(), name='api_settings'),
    path('webhook/orders/', views.webhook_orders, name='webhook_orders'),
]
from django.urls import path

from . import views

app_name = 'companies'

urlpatterns = [
    path('', views.CompanyList.as_view(), name='list'),
    path('new/', views.CompanyCreate.as_view(), name='create'),
    path('<int:pk>/edit/', views.CompanyUpdate.as_view(), name='update'),
    path('<int:pk>/delete/', views.CompanyDelete.as_view(), name='delete'),
    path('api/', views.api_company_list, name='api_list'),
    path('api/new/', views.api_company_save, name='api_create'),
    path('api/<int:pk>/', views.api_company_save, name='api_update'),
    path('api/<int:pk>/delete/', views.api_company_delete, name='api_delete'),
]
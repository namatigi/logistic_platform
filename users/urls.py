from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = 'users'

urlpatterns = [
    path('login/', views.email_login, name='login'),
    path('signup/', views.signup, name='signup'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('admin/', views.admin_dashboard, name='admin_dashboard'),
    path('api/admin/dashboard/', views.api_admin_dashboard, name='api_admin_dashboard'),
    path('api/admin/agents/create/', views.api_admin_agent_create, name='api_admin_agent_create'),
    path('api/admin/agents/<int:pk>/transporters/', views.api_admin_agent_transporters, name='api_admin_agent_transporters'),
    path('agents/awarded/', views.agent_awarded, name='agent_awarded'),
    path('agents/tracker/', views.agent_tracker, name='agent_tracker'),
    path('agents/invoices/', views.agent_invoices, name='agent_invoices'),
    path('agents/transporters/', views.agent_transporters, name='agent_transporters'),
    path('api/agents/awarded/', views.api_agent_awarded, name='api_agent_awarded'),
    path('api/agents/tracker/', views.api_agent_tracker, name='api_agent_tracker'),
    path('api/agents/invoices/', views.api_agent_invoices, name='api_agent_invoices'),
    path('api/agents/invoices/<int:pk>/paid/', views.api_agent_invoice_paid, name='api_agent_invoice_paid'),
    path('api/agents/transporters/', views.api_agent_transporters, name='api_agent_transporters'),
    path('api/agents/transporters/<int:pk>/trucks/', views.api_agent_transporter_trucks, name='api_agent_transporter_trucks'),
    path('api/agents/truck-models/', views.api_agent_truck_models, name='api_agent_truck_models'),
    path('api/agents/truck-models/create/', views.api_agent_truck_models_create, name='api_agent_truck_models_create'),
    path('api/login/', views.api_login, name='api_login'),
    path('api/signup/', views.api_signup, name='api_signup'),
    path('api/profile/', views.api_profile, name='api_profile'),
    path('api/profile/save/', views.api_profile_save, name='api_profile_save'),
    path('api/addresses/save/', views.api_address_save, name='api_address_save'),
    path('api/addresses/', views.api_address_save, name='api_address_create'),
    path('api/addresses/<int:pk>/', views.api_address_save, name='api_address_update'),
    path('api/addresses/<int:pk>/delete/', views.api_address_delete, name='api_address_delete'),
]
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import LogoutView
from django.urls import path, reverse_lazy

from . import views

app_name = 'users'

urlpatterns = [
    path('login/', views.email_login, name='login'),
    path('google/login/', views.google_login, name='google_login'),
    path('google/callback/', views.google_callback, name='google_callback'),
    path('signup/', views.signup, name='signup'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='registration/password_reset_form.html',
        email_template_name='registration/password_reset_email.html',
        subject_template_name='registration/password_reset_subject.txt',
        success_url=reverse_lazy('users:password_reset_done'),
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='registration/password_reset_done.html',
    ), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='registration/password_reset_confirm.html',
        success_url=reverse_lazy('users:password_reset_complete'),
    ), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='registration/password_reset_complete.html',
    ), name='password_reset_complete'),
    path('profile/', views.profile_view, name='profile'),
    path('admin/', views.admin_dashboard, name='admin_dashboard'),
    path('api/admin/dashboard/', views.api_admin_dashboard, name='api_admin_dashboard'),
    path('api/admin/agents/create/', views.api_admin_agent_create, name='api_admin_agent_create'),
    path('api/admin/agents/<int:pk>/update/', views.api_admin_agent_update, name='api_admin_agent_update'),
    path('api/admin/agents/<int:pk>/transporters/', views.api_admin_agent_transporters, name='api_admin_agent_transporters'),
    path('agents/awarded/', views.agent_awarded, name='agent_awarded'),
    path('agents/tracker/', views.agent_tracker, name='agent_tracker'),
    path('agents/invoices/', views.agent_invoices, name='agent_invoices'),
    path('agents/transporters/', views.agent_transporters, name='agent_transporters'),
    path('agents/trucks/', views.agent_trucks, name='agent_trucks'),
    path('agents/trucks/<int:pk>/track/', views.agent_truck_track, name='agent_truck_track'),
    path('api/agents/awarded/', views.api_agent_awarded, name='api_agent_awarded'),
    path('api/agents/tracker/', views.api_agent_tracker, name='api_agent_tracker'),
    path('api/agents/invoices/', views.api_agent_invoices, name='api_agent_invoices'),
    path('api/agents/invoices/<int:pk>/paid/', views.api_agent_invoice_paid, name='api_agent_invoice_paid'),
    path('api/agents/transporters/', views.api_agent_transporters, name='api_agent_transporters'),
    path('api/agents/transporters/<int:pk>/trucks/', views.api_agent_transporter_trucks, name='api_agent_transporter_trucks'),
    path('api/agents/trucks/', views.api_agent_trucks, name='api_agent_trucks'),
    path('api/agents/trucks/<int:pk>/track/', views.api_agent_truck_track, name='api_agent_truck_track'),
    path('api/agents/trucks/<int:pk>/delete/', views.api_agent_truck_delete, name='api_agent_truck_delete'),
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
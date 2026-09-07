from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = 'users'

urlpatterns = [
    path('login/', views.email_login, name='login'),
    path('signup/', views.signup, name='signup'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('api/login/', views.api_login, name='api_login'),
    path('api/signup/', views.api_signup, name='api_signup'),
    path('api/profile/', views.api_profile, name='api_profile'),
    path('api/profile/save/', views.api_profile_save, name='api_profile_save'),
    path('api/addresses/save/', views.api_address_save, name='api_address_save'),
    path('api/addresses/', views.api_address_save, name='api_address_create'),
    path('api/addresses/<int:pk>/', views.api_address_save, name='api_address_update'),
    path('api/addresses/<int:pk>/delete/', views.api_address_delete, name='api_address_delete'),
]
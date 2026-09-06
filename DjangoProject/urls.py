from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('tenders.urls')),
    path('companies/', include('companies.urls')),
    path('accounts/', include('users.urls')),
]
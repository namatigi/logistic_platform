from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/orders/$', consumers.OrderListConsumer.as_asgi()),
    re_path(r'ws/orders/(?P<pk>\d+)/$', consumers.OrderDetailConsumer.as_asgi()),
]

from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.NotificationsView.as_view()),
    path("notifications/unread-count/", views.CountView.as_view()),
    path("notifications/<uuid:notification_id>/", views.NotificationDetailView.as_view()),
    path("notifications/<uuid:notification_id>/read/", views.ReadView.as_view()),
    path("notifications/devices/", views.DevicesView.as_view()),
    path("notifications/devices/<uuid:device_id>/", views.DeviceDeleteView.as_view()),
]

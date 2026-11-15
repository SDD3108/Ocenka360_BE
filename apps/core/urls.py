from django.urls import path

from .views import LiveView, PublicConfigView, ReadyView

app_name = "core"
urlpatterns = [
    path("health/live/", LiveView.as_view(), name="live"),
    path("health/ready/", ReadyView.as_view(), name="ready"),
    path("config/", PublicConfigView.as_view(), name="config"),
]

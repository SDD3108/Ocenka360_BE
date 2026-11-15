from django.urls import path

from .views import (
    LogoutAllView,
    LogoutView,
    RefreshView,
    RequestCodeView,
    SessionListView,
    SessionRevokeView,
    VerifyCodeView,
)

app_name = "auth"
urlpatterns = [
    path("request-code/", RequestCodeView.as_view(), name="request-code"),
    path("verify-code/", VerifyCodeView.as_view(), name="verify-code"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("sessions/", SessionListView.as_view(), name="sessions"),
    path("sessions/<uuid:session_id>/", SessionRevokeView.as_view(), name="revoke-session"),
]

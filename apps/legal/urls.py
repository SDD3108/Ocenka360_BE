from django.urls import path

from .views import ConsentsView, DocumentsView

app_name = "legal"
urlpatterns = [
    path("documents/", DocumentsView.as_view(), name="documents"),
    path("consents/", ConsentsView.as_view(), name="consents"),
]

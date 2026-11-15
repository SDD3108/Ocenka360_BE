from django.urls import path

from .views import CategoryListView, ServiceDetailView, ServiceFormView, ServiceListView, ValidateFormView

app_name = "catalog"
urlpatterns = [
    path("categories/", CategoryListView.as_view(), name="categories"),
    path("services/", ServiceListView.as_view(), name="services"),
    path("services/<slug:slug>/", ServiceDetailView.as_view(), name="service-detail"),
    path("services/<slug:slug>/form/", ServiceFormView.as_view(), name="service-form"),
    path("services/<slug:slug>/validate/", ValidateFormView.as_view(), name="service-validate"),
]

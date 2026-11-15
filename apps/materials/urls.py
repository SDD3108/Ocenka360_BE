from django.urls import path

from . import views

urlpatterns = [
    path("orders/<uuid:order_id>/materials/", views.MaterialListView.as_view(), name="material-list"),
    path(
        "orders/<uuid:order_id>/materials/requirements/",
        views.MaterialRequirementsView.as_view(),
        name="material-requirements",
    ),
    path(
        "orders/<uuid:order_id>/materials/<uuid:material_id>/download/",
        views.MaterialDownloadView.as_view(),
        name="material-download",
    ),
    path(
        "orders/<uuid:order_id>/materials/<uuid:material_id>/withdraw/",
        views.MaterialWithdrawView.as_view(),
        name="material-withdraw",
    ),
    path(
        "review/orders/<uuid:order_id>/materials/",
        views.ReviewMaterialListView.as_view(),
        name="review-material-list",
    ),
    path(
        "review/orders/<uuid:order_id>/materials/requirements/",
        views.ReviewMaterialRequirementsView.as_view(),
        name="review-material-requirements",
    ),
    path(
        "review/orders/<uuid:order_id>/materials/<uuid:material_id>/download/",
        views.ReviewMaterialDownloadView.as_view(),
        name="review-material-download",
    ),
    path(
        "review/orders/<uuid:order_id>/materials/<uuid:material_id>/withdraw/",
        views.ReviewMaterialWithdrawView.as_view(),
        name="review-material-withdraw",
    ),
    path(
        "review/orders/<uuid:order_id>/materials/<uuid:material_id>/review/",
        views.MaterialReviewView.as_view(),
        name="material-review",
    ),
]

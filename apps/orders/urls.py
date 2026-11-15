from django.urls import path

from . import views

app_name = "orders"
urlpatterns = [
    path("orders/<uuid:order_id>/form/", views.OrderFormView.as_view(), name="order-form"),
    path("review/orders/<uuid:order_id>/form/", views.ReviewFormView.as_view(), name="review-form"),
    path("assets/", views.AssetListView.as_view(), name="assets"),
    path("assets/<uuid:asset_id>/", views.AssetDetailView.as_view(), name="asset-detail"),
    path("orders/", views.OrderListView.as_view(), name="orders"),
    path("orders/<uuid:order_id>/", views.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<uuid:order_id>/submit/", views.OrderSubmitView.as_view(), name="order-submit"),
    path("orders/<uuid:order_id>/cancel/", views.OrderCancelView.as_view(), name="order-cancel"),
    path("orders/<uuid:order_id>/history/", views.OrderHistoryView.as_view(), name="order-history"),
    path(
        "orders/<uuid:order_id>/submissions/", views.OrderSubmissionsView.as_view(), name="order-submissions"
    ),
    path("review/orders/", views.ReviewListView.as_view(), name="review-list"),
    path("review/orders/<uuid:order_id>/", views.ReviewDetailView.as_view(), name="review-detail"),
    path("review/orders/<uuid:order_id>/actions/", views.ReviewActionView.as_view(), name="review-actions"),
    path("review/orders/<uuid:order_id>/history/", views.ReviewHistoryView.as_view(), name="review-history"),
    path(
        "review/orders/<uuid:order_id>/submissions/",
        views.ReviewSubmissionsView.as_view(),
        name="review-submissions",
    ),
]

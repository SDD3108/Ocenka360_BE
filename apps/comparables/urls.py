from django.urls import path

from . import views

urlpatterns = [
    path("orders/<uuid:order_id>/market-estimates/", views.EstimatesView.as_view()),
    path("review/orders/<uuid:order_id>/market-estimates/", views.ReviewEstimatesView.as_view()),
    path(
        "review/orders/<uuid:order_id>/market-estimates/<uuid:estimate_id>/share/", views.ShareView.as_view()
    ),
    path(
        "review/orders/<uuid:order_id>/market-estimates/<uuid:estimate_id>/withdraw/",
        views.WithdrawView.as_view(),
    ),
]

from django.urls import path

from . import views

urlpatterns = [
    path("orders/<uuid:order_id>/reports/", views.ReportsView.as_view()),
    path("review/orders/<uuid:order_id>/reports/", views.ReviewReportsView.as_view()),
    path("review/orders/<uuid:order_id>/reports/<uuid:report_id>/publish/", views.PublishView.as_view()),
    path("review/orders/<uuid:order_id>/reports/<uuid:report_id>/withdraw/", views.WithdrawView.as_view()),
    path("orders/<uuid:order_id>/reports/<uuid:report_id>/download/", views.DownloadView.as_view()),
    path(
        "review/orders/<uuid:order_id>/reports/<uuid:report_id>/download/", views.ReviewDownloadView.as_view()
    ),
    path("orders/<uuid:order_id>/reports/<uuid:report_id>/original/", views.OriginalView.as_view()),
    path(
        "review/orders/<uuid:order_id>/reports/<uuid:report_id>/original/", views.ReviewOriginalView.as_view()
    ),
    path("orders/<uuid:order_id>/reports/<uuid:report_id>/delivery-request/", views.DeliveryView.as_view()),
]

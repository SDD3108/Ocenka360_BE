from django.urls import path

from . import views

urlpatterns = [
    path("review/payments/", views.PaymentQueueView.as_view(), name="payment-queue"),
    path("review/refunds/", views.RefundQueueView.as_view(), name="refund-queue"),
    path("review/cancellations/", views.CancellationQueueView.as_view(), name="cancellation-queue"),
    path("payments/methods/", views.MethodsView.as_view(), name="payment-methods"),
    path("payments/halyk/postlink/", views.PostlinkView.as_view(), name="halyk-postlink"),
    path("orders/<uuid:order_id>/payments/", views.PaymentsView.as_view(), name="order-payments"),
    path(
        "orders/<uuid:order_id>/payments/history/", views.PaymentHistoryView.as_view(), name="payment-history"
    ),
    path(
        "orders/<uuid:order_id>/payments/<uuid:intent_id>/checkout/",
        views.CheckoutView.as_view(),
        name="payment-checkout",
    ),
    path(
        "orders/<uuid:order_id>/payments/<uuid:intent_id>/refresh/",
        views.RefreshView.as_view(),
        name="payment-refresh",
    ),
    path(
        "review/orders/<uuid:order_id>/payments/", views.ReviewPaymentsView.as_view(), name="review-payments"
    ),
    path(
        "review/orders/<uuid:order_id>/payments/history/",
        views.ReviewPaymentHistoryView.as_view(),
        name="review-payment-history",
    ),
    path(
        "review/orders/<uuid:order_id>/payments/<uuid:intent_id>/confirm-manual/",
        views.ManualPaymentView.as_view(),
        name="manual-payment",
    ),
    path(
        "review/orders/<uuid:order_id>/payments/<uuid:intent_id>/refresh/",
        views.ReviewRefreshView.as_view(),
        name="review-payment-refresh",
    ),
    path(
        "review/orders/<uuid:order_id>/refunds/<uuid:refund_id>/execute/",
        views.RefundExecuteView.as_view(),
        name="refund-execute",
    ),
    path(
        "review/orders/<uuid:order_id>/refunds/<uuid:refund_id>/confirm-manual/",
        views.ManualRefundView.as_view(),
        name="manual-refund",
    ),
    path(
        "orders/<uuid:order_id>/cancellations/", views.CancellationView.as_view(), name="order-cancellations"
    ),
    path(
        "orders/<uuid:order_id>/cancellations/<uuid:case_id>/decide/",
        views.CaseDecisionView.as_view(),
        name="cancellation-decide",
    ),
    path(
        "review/orders/<uuid:order_id>/cancellations/",
        views.ReviewCancellationView.as_view(),
        name="review-cancellations",
    ),
    path(
        "review/orders/<uuid:order_id>/cancellations/<uuid:case_id>/offer/",
        views.CaseOfferView.as_view(),
        name="cancellation-offer",
    ),
]

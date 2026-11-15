from django.urls import path

from . import views

urlpatterns = [
    path("pricing/services/<slug:slug>/estimate/", views.EstimateView.as_view(), name="pricing-estimate"),
    path("orders/<uuid:order_id>/pricing/", views.OwnerPricingView.as_view(), name="order-pricing"),
    path(
        "orders/<uuid:order_id>/pricing/estimate/",
        views.OrderEstimateView.as_view(),
        name="order-price-estimate",
    ),
    path("orders/<uuid:order_id>/pricing/promo/", views.SelectPromoView.as_view(), name="order-promo"),
    path(
        "orders/<uuid:order_id>/pricing/<uuid:quote_id>/",
        views.QuoteDetailView.as_view(),
        name="quote-detail",
    ),
    path(
        "orders/<uuid:order_id>/pricing/<uuid:quote_id>/accept/",
        views.AcceptQuoteView.as_view(),
        name="quote-accept",
    ),
    path(
        "orders/<uuid:order_id>/pricing/<uuid:quote_id>/decline/",
        views.DeclineQuoteView.as_view(),
        name="quote-decline",
    ),
    path("review/orders/<uuid:order_id>/pricing/", views.ReviewPricingView.as_view(), name="review-pricing"),
    path(
        "review/orders/<uuid:order_id>/pricing/<uuid:quote_id>/",
        views.ReviewQuoteDetailView.as_view(),
        name="review-quote-detail",
    ),
    path(
        "review/orders/<uuid:order_id>/pricing/<uuid:quote_id>/withdraw/",
        views.WithdrawQuoteView.as_view(),
        name="quote-withdraw",
    ),
    path("management/pricing/tariffs/", views.TariffListView.as_view(), name="tariff-list"),
    path(
        "management/pricing/tariffs/<uuid:tariff_id>/publish/",
        views.PublishTariffView.as_view(),
        name="tariff-publish",
    ),
    path("management/pricing/promocodes/", views.PromoListView.as_view(), name="promo-list"),
    path(
        "management/pricing/promocodes/<uuid:promo_id>/", views.PromoToggleView.as_view(), name="promo-toggle"
    ),
]

from django.urls import path

from . import views

urlpatterns = [
    path("orders/<uuid:order_id>/schedule/", views.OrderScheduleView.as_view(), name="order-schedule"),
    path(
        "orders/<uuid:order_id>/schedule/history/",
        views.ScheduleHistoryView.as_view(),
        name="order-schedule-history",
    ),
    path(
        "orders/<uuid:order_id>/schedule/<uuid:appointment_id>/accept/",
        views.AcceptView.as_view(),
        name="schedule-accept",
    ),
    path(
        "orders/<uuid:order_id>/schedule/<uuid:appointment_id>/cancel/",
        views.CancelView.as_view(),
        name="schedule-cancel",
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/", views.ReviewScheduleView.as_view(), name="review-schedule"
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/history/",
        views.ReviewScheduleHistoryView.as_view(),
        name="review-schedule-history",
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/<uuid:appointment_id>/confirm/",
        views.ConfirmView.as_view(),
        name="schedule-confirm",
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/<uuid:appointment_id>/cancel/",
        views.ReviewCancelView.as_view(),
        name="schedule-review-cancel",
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/<uuid:appointment_id>/start/",
        views.StartView.as_view(),
        name="inspection-start",
    ),
    path(
        "review/orders/<uuid:order_id>/schedule/<uuid:appointment_id>/finish/",
        views.FinishView.as_view(),
        name="inspection-finish",
    ),
    path("review/calendar/", views.CalendarView.as_view(), name="calendar"),
    path("review/calendar/blocks/", views.BlockView.as_view(), name="calendar-block"),
    path(
        "review/calendar/blocks/<uuid:block_id>/cancel/",
        views.BlockCancelView.as_view(),
        name="calendar-block-cancel",
    ),
]

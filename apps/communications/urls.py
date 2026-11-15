from django.urls import path

from . import views

urlpatterns = [
    path("orders/<uuid:order_id>/comments/", views.CommentsView.as_view()),
    path("review/orders/<uuid:order_id>/comments/", views.ReviewCommentsView.as_view()),
    path("orders/<uuid:order_id>/material-requests/", views.RequestsView.as_view()),
    path("review/orders/<uuid:order_id>/material-requests/", views.ReviewRequestsView.as_view()),
    path("orders/<uuid:order_id>/material-requests/<uuid:request_id>/replies/", views.RepliesView.as_view()),
    path(
        "review/orders/<uuid:order_id>/material-requests/<uuid:request_id>/replies/",
        views.ReviewRepliesView.as_view(),
    ),
    path(
        "review/orders/<uuid:order_id>/material-requests/<uuid:request_id>/decide/",
        views.DecisionView.as_view(),
    ),
    path("review/material-requests/", views.RequestQueueView.as_view()),
]

from django.urls import path

from . import views

urlpatterns = [
    path("assessment-tools/", views.ToolsView.as_view()),
    path("orders/<uuid:order_id>/ai-permission/", views.PermissionView.as_view()),
    path("ai/jobs/", views.JobsView.as_view()),
    path("ai/jobs/<uuid:job_id>/", views.JobView.as_view()),
    path("ai/jobs/<uuid:job_id>/cancel/", views.CancelView.as_view()),
    path("review/ai/jobs/", views.ReviewJobsView.as_view()),
    path("review/ai/jobs/<uuid:job_id>/", views.ReviewJobView.as_view()),
    path("review/ai/jobs/<uuid:job_id>/cancel/", views.ReviewCancelView.as_view()),
    path("review/ai/jobs/<uuid:job_id>/decision/", views.DecisionView.as_view()),
]

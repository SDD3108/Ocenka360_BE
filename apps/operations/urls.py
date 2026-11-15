from django.urls import path

from . import views

urlpatterns = [
    path("review/tasks/", views.TasksView.as_view()),
    path("review/tasks/<uuid:task_id>/", views.TaskView.as_view()),
    path("review/tasks/<uuid:task_id>/history/", views.TaskHistoryView.as_view()),
    path("review/work-queue/", views.WorkQueueView.as_view()),
    path("review/dashboard/", views.SummaryView.as_view()),
]

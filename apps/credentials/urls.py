from django.urls import path

from . import views

app_name = "credentials"

urlpatterns = [
    path("", views.ai_providers_view, name="ai_providers"),
]

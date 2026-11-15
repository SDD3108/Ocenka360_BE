from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import ApiRootView

urlpatterns = [
    path("api/v1/", include("apps.payments.urls")),
    path("api/v1/", include("apps.scheduling.urls")),
    path("api/v1/", include("apps.pricing.urls")),
    path("api/v1/", include("apps.materials.urls")),
    path("api/v1/", include("apps.orders.urls")),
    path("api/v1/catalog/", include("apps.catalog.urls")),
    path("admin/", admin.site.urls),
    path("api/v1/", ApiRootView.as_view(), name="api-root"),
    path("api/v1/", include("apps.core.urls")),
    path("api/v1/auth/", include("apps.accounts.auth_urls")),
    path("api/v1/legal/", include("apps.legal.urls")),
    path("api/v1/accounts/", include("apps.accounts.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
handler400 = "apps.core.errors.bad_request"
handler403 = "apps.core.errors.permission_denied"
handler404 = "apps.core.errors.not_found"
handler500 = "apps.core.errors.server_error"
admin.site.site_header = "Оценка 360 — администрирование"
admin.site.site_title = "Оценка 360"
admin.site.index_title = "Управление системой"

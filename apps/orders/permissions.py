from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.permissions import HasAcceptedCurrentDocuments


class VerifiedAccount(BasePermission):
    message = "Verify your email first."
    code = "email_unverified"

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.email_verified_at)


class ConsentForWrites(HasAcceptedCurrentDocuments):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or super().has_permission(request, view)

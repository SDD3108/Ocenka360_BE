from rest_framework.permissions import BasePermission

from .models import User


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.role == User.Role.ADMIN)


class IsAppraiserOrAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.role in {User.Role.APPRAISER, User.Role.ADMIN}
        )


class HasAcceptedCurrentDocuments(BasePermission):
    """Combine with ownership/role checks on future order and AI endpoints."""

    message = "Accept the current documents and confirm you are an adult."
    code = "consent_required"

    def has_permission(self, request, view):
        from apps.legal.services import requires_consent

        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.email_verified_at
            and not requires_consent(user)
        )

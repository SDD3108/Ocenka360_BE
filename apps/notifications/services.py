from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import AuthSession, User
from apps.accounts.security import reserve_quota
from apps.orders import services as orders

from .messages import ACTIONS
from .models import DeviceLock, Notification, PushDelivery, PushDevice

REVIEW_ROLES = {User.Role.APPRAISER, User.Role.ADMIN}


def visible(actor):
    query = Notification.objects.filter(recipient=actor)
    access = Q(audience="client", event__order__owner=actor)
    if actor.role in REVIEW_ROLES:
        access |= Q(audience="review", event__order__review_unlocked_at__isnull=False)
    query = query.filter(access)
    if not settings.ALLOW_ORDER_SIMULATION:
        query = query.filter(event__order__is_test=False)
    return query.select_related("event__order")


def notify_event(event):
    # Called inside the business transaction, never from on_commit or via the network.
    order = event.order
    if event.action not in ACTIONS or not order.review_unlocked_at:
        return
    recipients = {order.owner_id: "client"}
    for user_id in User.objects.filter(
        is_active=True, email_verified_at__isnull=False, role__in=REVIEW_ROLES
    ).values_list("id", flat=True):
        if user_id != order.owner_id:
            recipients[user_id] = "review"
    for user_id, audience in recipients.items():
        if user_id == event.actor_id:
            continue
        kind = (
            "new_order"
            if audience == "review"
            and event.from_status == "awaiting_payment"
            and event.to_status == "submitted"
            else event.action
        )
        notification, created = Notification.objects.get_or_create(
            recipient_id=user_id, event=event, defaults={"audience": audience, "kind": kind}
        )
        if created:
            devices = PushDevice.objects.filter(
                user_id=user_id,
                active=True,
                session__revoked_at__isnull=True,
                session__expires_at__gt=timezone.now(),
            )
            PushDelivery.objects.bulk_create(
                [
                    PushDelivery(
                        notification=notification,
                        device=d,
                        device_revision=d.revision,
                        next_attempt_at=timezone.now(),
                    )
                    for d in devices
                ]
            )


def session_for(actor, session_id):
    session = (
        AuthSession.objects.filter(
            pk=session_id, user=actor, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).first()
        if session_id
        else None
    )
    if not session:
        raise PermissionDenied("An active JWT session is required.")
    return session


@transaction.atomic
def register_device(actor, session_id, installation_id, token, platform, project_id):
    actor = orders.actor_for_write(actor, consent=False)
    session = session_for(actor, session_id)
    if str(project_id) != settings.EXPO_PROJECT_ID:
        raise ValidationError({"project_id": "Use this application's configured Expo project."})
    reserve_quota("push-register", str(actor.pk), 20, 3600)
    DeviceLock.objects.select_for_update().get(pk=1)
    existing = PushDevice.objects.filter(installation_id=installation_id).first()
    token_owner = PushDevice.objects.filter(token=token).first()
    if token_owner and (not existing or token_owner.pk != existing.pk):
        raise orders.Conflict("Device registration conflicts. Remove the previous registration first.")
    if existing and existing.user_id != actor.pk:
        # A shared installation can be rebound only after the old session is revoked/expired,
        # and possession of the same private Expo token is required.
        if existing.token != token or (
            existing.active
            and existing.session.revoked_at is None
            and existing.session.expires_at > timezone.now()
        ):
            raise orders.Conflict("Sign out of the previous account on this installation first.")
    if (not existing or existing.user_id != actor.pk or not existing.active) and PushDevice.objects.filter(
        user=actor, active=True
    ).count() >= 5:
        raise orders.Conflict("At most five active push devices are allowed.")
    if (
        existing
        and existing.user_id == actor.pk
        and existing.session_id == session.pk
        and existing.token == token
        and existing.active
        and existing.platform == platform
    ):
        return existing
    if not existing:
        existing = PushDevice(installation_id=installation_id)
    else:
        existing.revision += 1
    existing.user, existing.session = actor, session
    existing.token, existing.platform, existing.project_id, existing.active = (
        token,
        platform,
        project_id,
        True,
    )
    existing.save()
    return existing


@transaction.atomic
def unregister_device(actor, device_id):
    actor = orders.actor_for_write(actor, consent=False)
    DeviceLock.objects.select_for_update().get(pk=1)
    device = get_object_or_404(PushDevice, pk=device_id, user=actor)
    if device.active:
        device.active = False
        device.revision += 1
        device.save(update_fields=["active", "revision", "updated_at"])


def delivery_allowed(delivery):
    device = delivery.device
    n = delivery.notification
    now = timezone.now()
    return bool(
        device
        and device.active
        and device.revision == delivery.device_revision
        and device.user_id == n.recipient_id
        and device.user.is_active
        and device.user.email_verified_at
        and device.session.revoked_at is None
        and device.session.expires_at > now
        and str(device.project_id) == settings.EXPO_PROJECT_ID
        and n.read_at is None
        and visible(device.user).filter(pk=n.pk).exists()
    )

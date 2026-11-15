from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.accounts.models import User

from .models import ConsentAcceptance, LegalDocument


class LegalNotReady(APIException):
    status_code = 503
    default_code = "legal_not_ready"
    default_detail = "Required documents are not published yet."


def required_documents(language):
    query = LegalDocument.objects.filter(is_active=True, language=language)
    if not settings.ALLOW_DRAFT_LEGAL_DOCUMENTS:
        if not settings.COMPANY_LEGAL_NAME:
            raise LegalNotReady()
        query = query.filter(is_draft=False)
    documents = list(query)
    if {doc.kind for doc in documents} != set(LegalDocument.Kind.values):
        raise LegalNotReady()
    return documents


def requires_consent(user):
    if not user.adult_confirmed_at:
        return True
    try:
        documents = required_documents(user.preferred_language)
    except LegalNotReady:
        return True
    return user.consents.filter(document__in=documents).count() != len(documents)


@transaction.atomic
def accept_documents(user, ids, adult_confirmed):
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.email_verified_at:
        raise ValidationError({"email": "Verify your email before accepting documents."})
    if not adult_confirmed:
        raise ValidationError({"adult_confirmed": "Adult confirmation is required."})
    # Lock active versions until the accept transaction commits.
    documents = required_documents(user.preferred_language)
    list(LegalDocument.objects.select_for_update().filter(pk__in=[doc.pk for doc in documents]))
    documents = required_documents(user.preferred_language)
    if set(ids) != {doc.id for doc in documents} or len(ids) != len(documents):
        raise ValidationError(
            {"documents": "Fetch and accept the current document versions."}, code="stale_documents"
        )
    for document in documents:
        ConsentAcceptance.objects.get_or_create(
            user=user,
            document=document,
            defaults={"document_checksum": document.checksum, "adult_confirmed": True},
        )
    if not user.adult_confirmed_at:
        user.adult_confirmed_at = timezone.now()
        user.save(update_fields=["adult_confirmed_at", "updated_at"])
    return user

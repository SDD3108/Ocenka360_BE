import ipaddress
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .models import Estimate


class ComparableInput(StrictSerializer):
    source_url = serializers.URLField(max_length=500)
    source_label = serializers.CharField(max_length=100)
    observed_on = serializers.DateField()
    city = serializers.CharField(max_length=100)
    area_sqm = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=5, max_value=2000)
    asking_price_kzt = serializers.DecimalField(
        max_digits=16, decimal_places=2, min_value=100000, max_value=100000000000
    )
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")

    def validate_source_url(self, value):
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise serializers.ValidationError("Invalid source URL.") from None
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or port not in {None, 443}
        ):
            raise serializers.ValidationError(
                "Use a public HTTPS listing URL without credentials or custom ports."
            )
        host = parts.hostname.lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise serializers.ValidationError("IP-address URLs are not allowed.")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            raise serializers.ValidationError("Use a public listing domain.")
        # Stored as evidence only; server never fetches any submitted URL.
        return urlunsplit(("https", host, parts.path.rstrip("/") or "/", parts.query, ""))


class CreateInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    client_request_id = serializers.UUIDField()
    as_of = serializers.DateField()
    comparability_confirmed = serializers.BooleanField()
    comparables = ComparableInput(many=True, min_length=3, max_length=10)
    note = serializers.CharField(max_length=500, required=False, default="", allow_blank=True)

    def validate(self, data):
        if not data["comparability_confirmed"]:
            raise serializers.ValidationError(
                "The appraiser must confirm comparability and lawful use of sources."
            )
        if data["as_of"] > timezone.localdate():
            raise serializers.ValidationError("Future dates are not supported.")
        rows = data["comparables"]
        if len({x["source_url"] for x in rows}) != len(rows):
            raise serializers.ValidationError("Duplicate source URL.")
        for row in rows:
            if not 0 <= (data["as_of"] - row["observed_on"]).days <= 90:
                raise serializers.ValidationError("Each observation must be within 90 days before as_of.")
        return data


class ActionInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    confirmed = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, default="", allow_blank=True)


class MarketEstimateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Estimate
        fields = (
            "id",
            "order_id",
            "state",
            "version",
            "order_version",
            "snapshot",
            "result",
            "created_at",
            "shared_at",
            "withdrawn_at",
        )
        read_only_fields = fields


class EstimateQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)

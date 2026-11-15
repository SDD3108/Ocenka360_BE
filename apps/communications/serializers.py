from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .models import Comment, MaterialReply, MaterialRequest


class CommentInput(StrictSerializer):
    client_request_id = serializers.UUIDField()
    body = serializers.CharField(max_length=2000)


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ("id", "author_role", "body", "client_request_id", "created_at")
        read_only_fields = fields


class ItemInput(StrictSerializer):
    kind = serializers.ChoiceField(choices=["document", "photo", "text"])
    slot_key = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")
    instruction = serializers.CharField(max_length=1000)
    where_to_get = serializers.CharField(max_length=1000, allow_blank=True, required=False, default="")

    def validate(self, data):
        if (data["kind"] == "text") != (not data["slot_key"]):
            raise serializers.ValidationError("Text uses no slot; documents and photos require a slot.")
        return data


class RequestInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    client_request_id = serializers.UUIDField()
    message = serializers.CharField(max_length=2000)
    items = ItemInput(many=True, allow_empty=False, max_length=20)

    def validate_items(self, items):
        keys = [(i["kind"], i["slot_key"]) for i in items]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError("Duplicate items are not allowed.")
        return items


class ReplyInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    client_request_id = serializers.UUIDField()
    body = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    material_ids = serializers.ListField(child=serializers.UUIDField(), max_length=30, allow_empty=True)
    confirmed = serializers.BooleanField()

    def validate_material_ids(self, ids):
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Duplicate file ids.")
        return ids


class DecisionInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    decision = serializers.ChoiceField(choices=["accept", "return", "cancel"])
    comment = serializers.CharField(max_length=2000)


class RequestSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)

    class Meta:
        model = MaterialRequest
        fields = (
            "id",
            "order_id",
            "order_number",
            "message",
            "items",
            "state",
            "review_comment",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ReplySerializer(serializers.ModelSerializer):
    material_ids = serializers.SerializerMethodField()

    def get_material_ids(self, obj) -> list[str]:
        return [str(x.material_id) for x in obj.links.all()]

    class Meta:
        model = MaterialReply
        fields = ("id", "request_id", "body", "material_ids", "client_request_id", "created_at")
        read_only_fields = fields


class CommentResult(serializers.Serializer):
    comment = CommentSerializer()
    order_version = serializers.IntegerField()


class RequestResult(serializers.Serializer):
    request = RequestSerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()


class ReplyResult(serializers.Serializer):
    reply = ReplySerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()

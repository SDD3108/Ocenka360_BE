from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Comment, MaterialReply, MaterialRequest, ReplyMaterial

for model in (Comment, MaterialRequest, MaterialReply, ReplyMaterial):
    admin.site.register(model, ReadOnlyAdmin)

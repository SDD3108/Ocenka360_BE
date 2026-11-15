from drf_spectacular.utils import extend_schema
from rest_framework.generics import RetrieveAPIView

from apps.core.serializers import ErrorSerializer

from .serializers import CurrentUserSerializer


@extend_schema(responses={200: CurrentUserSerializer, 403: ErrorSerializer}, tags=["Accounts"])
class CurrentUserView(RetrieveAPIView):
    serializer_class = CurrentUserSerializer

    def get_object(self):
        return self.request.user

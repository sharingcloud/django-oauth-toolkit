from django.db import models

from .models import get_access_token_model, get_id_token_model, get_refresh_token_model


def invalidate_tokens_for_user(user):
    if not user or not user.id:
        return
    access_tokens = get_access_token_model().objects.filter(user=user)
    refresh_tokens = get_refresh_token_model().objects.filter(access_token__in=access_tokens)
    id_tokens = get_id_token_model().objects.filter(
        models.Q(id__in=list(set(access_tokens.values_list("id_token", flat=True)))) | models.Q(user=user)
    )
    refresh_tokens.delete()
    access_tokens.delete()
    id_tokens.delete()

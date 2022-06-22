from django.dispatch import Signal


app_authorized = Signal()  # providing_args=["request", "token"]

oidc_session_ended = Signal()  # providing_args=["request", "user"]

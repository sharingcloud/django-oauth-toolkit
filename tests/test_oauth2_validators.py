import contextlib
import datetime
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from jwcrypto import jwt
from oauthlib.common import Request

from oauth2_provider.exceptions import FatalClientError
from oauth2_provider.models import get_access_token_model, get_application_model, get_refresh_token_model
from oauth2_provider.oauth2_backends import get_oauthlib_core
from oauth2_provider.oauth2_validators import OAuth2Validator

from . import presets
from .utils import get_basic_auth_header


try:
    from unittest import mock
except ImportError:
    import mock


UserModel = get_user_model()
Application = get_application_model()
AccessToken = get_access_token_model()
RefreshToken = get_refresh_token_model()

CLEARTEXT_SECRET = "1234567890abcdefghijklmnopqrstuvwxyz"


@contextlib.contextmanager
def always_invalid_token():
    # NOTE: This can happen if someone swaps the AccessToken model and
    # updates `is_valid` such that it has some criteria on top of
    # `is_expired` and `allow_scopes`.
    original_is_valid = AccessToken.is_valid
    AccessToken.is_valid = mock.MagicMock(return_value=False)
    try:
        yield
    finally:
        AccessToken.is_valid = original_is_valid


class TestOAuth2Validator(TransactionTestCase):
    def setUp(self):
        self.user = UserModel.objects.create_user("user", "test@example.com", "123456")
        self.request = mock.MagicMock(wraps=Request)
        self.request.user = self.user
        self.request.grant_type = "not client"
        self.validator = OAuth2Validator()
        self.application = Application.objects.create(
            client_id="client_id",
            client_secret=CLEARTEXT_SECRET,
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_PASSWORD,
        )
        self.request.client = self.application

    def tearDown(self):
        self.application.delete()

    def test_authenticate_request_body(self):
        self.request.client_id = "client_id"
        self.request.client_secret = ""
        self.assertFalse(self.validator._authenticate_request_body(self.request))

        self.request.client_secret = "wrong_client_secret"
        self.assertFalse(self.validator._authenticate_request_body(self.request))

        self.request.client_secret = CLEARTEXT_SECRET
        self.assertTrue(self.validator._authenticate_request_body(self.request))

    def test_extract_basic_auth(self):
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic 123456"}
        self.assertEqual(self.validator._extract_basic_auth(self.request), "123456")
        self.request.headers = {}
        self.assertIsNone(self.validator._extract_basic_auth(self.request))
        self.request.headers = {"HTTP_AUTHORIZATION": "Dummy 123456"}
        self.assertIsNone(self.validator._extract_basic_auth(self.request))
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic"}
        self.assertIsNone(self.validator._extract_basic_auth(self.request))
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic 123456 789"}
        self.assertEqual(self.validator._extract_basic_auth(self.request), "123456 789")

    def test_authenticate_basic_auth(self):
        self.request.encoding = "utf-8"
        self.request.headers = get_basic_auth_header("client_id", CLEARTEXT_SECRET)
        self.assertTrue(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_default_encoding(self):
        self.request.encoding = None
        self.request.headers = get_basic_auth_header("client_id", CLEARTEXT_SECRET)
        self.assertTrue(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_wrong_client_id(self):
        self.request.encoding = "utf-8"
        self.request.headers = get_basic_auth_header("wrong_id", CLEARTEXT_SECRET)
        self.assertFalse(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_wrong_client_secret(self):
        self.request.encoding = "utf-8"
        self.request.headers = get_basic_auth_header("client_id", "wrong_secret")
        self.assertFalse(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_not_b64_auth_string(self):
        self.request.encoding = "utf-8"
        # Can"t b64decode
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic not_base64"}
        self.assertFalse(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_invalid_b64_string(self):
        self.request.encoding = "utf-8"
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic ZHVtbXk=:ZHVtbXk=\n"}
        self.assertFalse(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_basic_auth_not_utf8(self):
        self.request.encoding = "utf-8"
        # b64decode("test") will become b"\xb5\xeb-", it can"t be decoded as utf-8
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic test"}
        self.assertFalse(self.validator._authenticate_basic_auth(self.request))

    def test_authenticate_client_id(self):
        self.assertTrue(self.validator.authenticate_client_id("client_id", self.request))

    def test_authenticate_client_id_fail(self):
        self.application.client_type = Application.CLIENT_CONFIDENTIAL
        self.application.save()
        self.assertFalse(self.validator.authenticate_client_id("client_id", self.request))
        self.assertFalse(self.validator.authenticate_client_id("fake_client_id", self.request))

    def test_client_authentication_required(self):
        self.request.headers = {"HTTP_AUTHORIZATION": "Basic 123456"}
        self.assertTrue(self.validator.client_authentication_required(self.request))
        self.request.headers = {}
        self.request.client_id = "client_id"
        self.request.client_secret = CLEARTEXT_SECRET
        self.assertTrue(self.validator.client_authentication_required(self.request))
        self.request.client_secret = ""
        self.assertFalse(self.validator.client_authentication_required(self.request))
        self.application.client_type = Application.CLIENT_CONFIDENTIAL
        self.application.save()
        self.request.client = ""
        self.assertTrue(self.validator.client_authentication_required(self.request))

    def test_load_application_fails_when_request_has_no_client(self):
        self.assertRaises(AssertionError, self.validator.authenticate_client_id, "client_id", {})

    def test_rotate_refresh_token__is_true(self):
        self.assertTrue(self.validator.rotate_refresh_token(mock.MagicMock()))

    def test_save_bearer_token__without_user__raises_fatal_client(self):
        token = {}

        with self.assertRaises(FatalClientError):
            self.validator.save_bearer_token(token, mock.MagicMock())

    def test_save_bearer_token__with_existing_tokens__does_not_create_new_tokens(self):

        rotate_token_function = mock.MagicMock()
        rotate_token_function.return_value = False
        self.validator.rotate_refresh_token = rotate_token_function

        access_token = AccessToken.objects.create(
            token="123",
            user=self.user,
            expires=timezone.now() + datetime.timedelta(seconds=60),
            application=self.application,
        )
        refresh_token = RefreshToken.objects.create(
            access_token=access_token, token="abc", user=self.user, application=self.application
        )
        self.request.refresh_token_instance = refresh_token
        token = {
            "scope": "foo bar",
            "refresh_token": "abc",
            "access_token": "123",
        }

        self.assertEqual(1, RefreshToken.objects.count())
        self.assertEqual(1, AccessToken.objects.count())

        self.validator.save_bearer_token(token, self.request)

        self.assertEqual(1, RefreshToken.objects.count())
        self.assertEqual(1, AccessToken.objects.count())

    def test_save_bearer_token__checks_to_rotate_tokens(self):

        rotate_token_function = mock.MagicMock()
        rotate_token_function.return_value = False
        self.validator.rotate_refresh_token = rotate_token_function

        access_token = AccessToken.objects.create(
            token="123",
            user=self.user,
            expires=timezone.now() + datetime.timedelta(seconds=60),
            application=self.application,
        )
        refresh_token = RefreshToken.objects.create(
            access_token=access_token, token="abc", user=self.user, application=self.application
        )
        self.request.refresh_token_instance = refresh_token
        token = {
            "scope": "foo bar",
            "refresh_token": "abc",
            "access_token": "123",
        }

        self.validator.save_bearer_token(token, self.request)
        rotate_token_function.assert_called_once_with(self.request)

    def test_save_bearer_token__with_new_token__creates_new_tokens(self):
        token = {
            "scope": "foo bar",
            "refresh_token": "abc",
            "access_token": "123",
        }

        self.assertEqual(0, RefreshToken.objects.count())
        self.assertEqual(0, AccessToken.objects.count())

        self.validator.save_bearer_token(token, self.request)

        self.assertEqual(1, RefreshToken.objects.count())
        self.assertEqual(1, AccessToken.objects.count())

    def test_save_bearer_token__with_new_token_equal_to_existing_token__revokes_old_tokens(self):
        access_token = AccessToken.objects.create(
            token="123",
            user=self.user,
            expires=timezone.now() + datetime.timedelta(seconds=60),
            application=self.application,
        )
        refresh_token = RefreshToken.objects.create(
            access_token=access_token, token="abc", user=self.user, application=self.application
        )

        self.request.refresh_token_instance = refresh_token

        token = {
            "scope": "foo bar",
            "refresh_token": "abc",
            "access_token": "123",
        }

        self.assertEqual(1, RefreshToken.objects.count())
        self.assertEqual(1, AccessToken.objects.count())

        self.validator.save_bearer_token(token, self.request)

        self.assertEqual(1, RefreshToken.objects.filter(revoked__isnull=True).count())
        self.assertEqual(1, AccessToken.objects.count())

    def test_save_bearer_token__with_no_refresh_token__creates_new_access_token_only(self):
        token = {
            "scope": "foo bar",
            "access_token": "123",
        }

        self.validator.save_bearer_token(token, self.request)

        self.assertEqual(0, RefreshToken.objects.count())
        self.assertEqual(1, AccessToken.objects.count())

    def test_save_bearer_token__with_new_token__calls_methods_to_create_access_and_refresh_tokens(self):
        token = {
            "scope": "foo bar",
            "refresh_token": "abc",
            "access_token": "123",
        }
        # Mock private methods to create access and refresh tokens
        create_access_token_mock = mock.MagicMock()
        create_refresh_token_mock = mock.MagicMock()
        self.validator._create_refresh_token = create_refresh_token_mock
        self.validator._create_access_token = create_access_token_mock

        self.validator.save_bearer_token(token, self.request)

        assert create_access_token_mock.call_count == 1
        assert create_refresh_token_mock.call_count == 1


class TestOAuth2ValidatorProvidesErrorData(TransactionTestCase):
    """These test cases check that the recommended error codes are returned
    when token authentication fails.

    RFC-6750: https://tools.ietf.org/html/rfc6750

    > If the protected resource request does not include authentication
    > credentials or does not contain an access token that enables access
    > to the protected resource, the resource server MUST include the HTTP
    > "WWW-Authenticate" response header field[.]
    >
    > ...
    >
    > If the request lacks any authentication information..., the
    > resource server SHOULD NOT include an error code or other error
    > information.
    >
    > ...
    >
    > If the protected resource request included an access token and failed
    > authentication, the resource server SHOULD include the "error"
    > attribute to provide the client with the reason why the access
    > request was declined.

    See https://tools.ietf.org/html/rfc6750#section-3.1 for the allowed error
    codes.
    """

    def setUp(self):
        self.user = UserModel.objects.create_user(
            "user",
            "test@example.com",
            "123456",
        )
        self.request = mock.MagicMock(wraps=Request)
        self.request.user = self.user
        self.request.grant_type = "not client"
        self.validator = OAuth2Validator()
        self.application = Application.objects.create(
            client_id="client_id",
            client_secret=CLEARTEXT_SECRET,
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_PASSWORD,
        )
        self.request.client = self.application

    def test_validate_bearer_token_does_not_add_error_when_no_token_is_provided(self):
        self.assertFalse(self.validator.validate_bearer_token(None, ["dolphin"], self.request))
        with self.assertRaises(AttributeError):
            self.request.oauth2_error

    def test_validate_bearer_token_adds_error_to_the_request_when_an_invalid_token_is_provided(self):
        access_token = mock.MagicMock(token="some_invalid_token")
        self.assertFalse(
            self.validator.validate_bearer_token(
                access_token.token,
                [],
                self.request,
            )
        )
        self.assertDictEqual(
            self.request.oauth2_error,
            {
                "error": "invalid_token",
                "error_description": "The access token is invalid.",
            },
        )

    def test_validate_bearer_token_adds_error_to_the_request_when_an_expired_token_is_provided(self):
        access_token = AccessToken.objects.create(
            token="some_valid_token",
            user=self.user,
            expires=timezone.now() - datetime.timedelta(seconds=1),
            application=self.application,
        )
        self.assertFalse(
            self.validator.validate_bearer_token(
                access_token.token,
                [],
                self.request,
            )
        )
        self.assertDictEqual(
            self.request.oauth2_error,
            {
                "error": "invalid_token",
                "error_description": "The access token has expired.",
            },
        )

    def test_validate_bearer_token_adds_error_to_the_request_when_a_valid_token_has_insufficient_scope(self):
        access_token = AccessToken.objects.create(
            token="some_valid_token",
            user=self.user,
            expires=timezone.now() + datetime.timedelta(seconds=1),
            application=self.application,
        )
        self.assertFalse(
            self.validator.validate_bearer_token(
                access_token.token,
                ["some_extra_scope"],
                self.request,
            )
        )
        self.assertDictEqual(
            self.request.oauth2_error,
            {
                "error": "insufficient_scope",
                "error_description": "The access token is valid but does not have enough scope.",
            },
        )

    def test_validate_bearer_token_adds_error_to_the_request_when_a_invalid_custom_token_is_provided(self):
        access_token = AccessToken.objects.create(
            token="some_valid_token",
            user=self.user,
            expires=timezone.now() + datetime.timedelta(seconds=1),
            application=self.application,
        )
        with always_invalid_token():
            self.assertFalse(
                self.validator.validate_bearer_token(
                    access_token.token,
                    [],
                    self.request,
                )
            )
        self.assertDictEqual(
            self.request.oauth2_error,
            {
                "error": "invalid_token",
            },
        )


class TestOAuth2ValidatorErrorResourceToken(TestCase):
    """The following tests check logger information when response from oauth2
    is unsuccessful.
    """

    def setUp(self):
        self.token = "test_token"
        self.introspection_url = "http://example.com/token/introspection/"
        self.introspection_token = "test_introspection_token"
        self.validator = OAuth2Validator()

    def test_response_when_auth_server_response_return_404(self):
        with self.assertLogs(logger="oauth2_provider") as mock_log:
            self.validator._get_token_from_authentication_server(
                self.token, self.introspection_url, self.introspection_token, None
            )
            self.assertIn(
                "ERROR:oauth2_provider:Introspection: Failed to "
                "get a valid response from authentication server. "
                "Status code: 404, Reason: "
                "Not Found.\nNoneType: None",
                mock_log.output,
            )


@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_oidc_endpoint_generation(oauth2_settings, rf):
    oauth2_settings.OIDC_ISS_ENDPOINT = ""
    django_request = rf.get("/")
    request = Request("/", headers=django_request.META)
    validator = OAuth2Validator()
    oidc_issuer_endpoint = validator.get_oidc_issuer_endpoint(request)
    assert oidc_issuer_endpoint == "http://testserver/"


@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_oidc_endpoint_generation_ssl(oauth2_settings, rf, settings):
    oauth2_settings.OIDC_ISS_ENDPOINT = ""
    django_request = rf.get("/", secure=True)
    # Calling the settings method with a django https request should generate a https url
    oidc_issuer_endpoint = oauth2_settings.oidc_issuer(django_request)
    assert oidc_issuer_endpoint == "https://testserver/"

    # Should also work with an oauthlib request (via validator)
    core = get_oauthlib_core()
    uri, http_method, body, headers = core._extract_params(django_request)
    request = Request(uri=uri, http_method=http_method, body=body, headers=headers)
    validator = OAuth2Validator()
    oidc_issuer_endpoint = validator.get_oidc_issuer_endpoint(request)
    assert oidc_issuer_endpoint == "https://testserver/"


@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_get_jwt_bearer_token(oauth2_settings, mocker):
    # oauthlib instructs us to make get_jwt_bearer_token call get_id_token
    request = mocker.MagicMock(wraps=Request)
    validator = OAuth2Validator()
    mock_get_id_token = mocker.patch.object(validator, "get_id_token")
    validator.get_jwt_bearer_token(None, None, request)
    assert mock_get_id_token.call_count == 1
    assert mock_get_id_token.call_args[0] == (None, None, request)
    assert mock_get_id_token.call_args[1] == {}


@pytest.mark.django_db
@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_validate_id_token_expired_jwt(oauth2_settings, mocker, oidc_tokens):
    mocker.patch("oauth2_provider.oauth2_validators.jwt.JWT", side_effect=jwt.JWTExpired)
    validator = OAuth2Validator()
    status = validator.validate_id_token(oidc_tokens.id_token, ["openid"], mocker.sentinel.request)
    assert status is False


@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_validate_id_token_no_token(oauth2_settings, mocker):
    validator = OAuth2Validator()
    status = validator.validate_id_token("", ["openid"], mocker.sentinel.request)
    assert status is False


@pytest.mark.django_db
@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_validate_id_token_app_removed(oauth2_settings, mocker, oidc_tokens):
    oidc_tokens.application.delete()
    validator = OAuth2Validator()
    status = validator.validate_id_token(oidc_tokens.id_token, ["openid"], mocker.sentinel.request)
    assert status is False


@pytest.mark.django_db
@pytest.mark.oauth2_settings(presets.OIDC_SETTINGS_RW)
def test_validate_id_token_bad_token_no_aud(oauth2_settings, mocker, oidc_key):
    token = jwt.JWT(header=json.dumps({"alg": "RS256"}), claims=json.dumps({"bad": "token"}))
    token.make_signed_token(oidc_key)
    validator = OAuth2Validator()
    status = validator.validate_id_token(token.serialize(), ["openid"], mocker.sentinel.request)
    assert status is False

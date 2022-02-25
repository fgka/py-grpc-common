# vim: ai:sw=4:ts=4:sta:et:fo=croql
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=missing-function-docstring,assignment-from-no-return,c-extension-no-member
# pylint: disable=protected-access,redefined-outer-name,no-self-use,using-constant-test
# pylint: disable=invalid-name,attribute-defined-outside-init,too-few-public-methods
# type: ignore
import logging
from typing import Any, Dict, List

import grpc
import pytest
import requests
import requests_mock

logging.getLogger().setLevel(logging.DEBUG)

from grpc_common import channel_factory

from . import example_service_pb2 as test_pb2_module


_AUTH_METADATA_PLUGIN_NAME: bytes = b'AccessTokenAuthMetadataPlugin'
_INNER_API_KEY_FN_NAME: bytes = b'_api_key_fn'

_TEST_URL_HTTP: str = 'http://localhost:5000'
_TEST_URL_HTTPS: str = 'https://localhost:5001'
_TEST_SA_EMAIL: str = 'service_account@gpc_project_id.iam.gserviceaccount.com'
_TEST_API_KEY: str = 'TEST_API_KEY'
_TEST_API_KEY_SECRET_FQN: str = (
    'projects/GCP_PROJECT_ID/secrets/SECRET_NAME/versions/SECRET_VERSION'
)
_TEST_JWT: str = 'TEST_JWT_TOKEN'

_TEST_ENDPOINT_URL: str = 'https://example.com:8443'
_TEST_CLEAN_HEALTH_URL_PATH: str = 'healthz'
_TEST_HEALTH_URL_PATH: str = f'/{_TEST_CLEAN_HEALTH_URL_PATH}/'


class _MockedSecretManagerServiceClient:
    class _StubbedSecretResponse:
        class _StubbedPayload:
            def __init__(self, secret_content: str):
                self.data = bytes(secret_content.encode('UTF-8'))

        def __init__(self, secret_content: str):
            self.payload = self._StubbedPayload(secret_content)

    def __init__(self, expected_secret_fqn: str, secret_content: str):
        self._expected_secret_fqn = expected_secret_fqn
        self._response = self._StubbedSecretResponse(secret_content)

    def access_secret_version(self, request: Dict[str, str]) -> _StubbedSecretResponse:
        assert request and request.get('name') == self._expected_secret_fqn
        return self._response


def test__get_jwt_token_url_ok():
    # Given
    client_service_account_email = _TEST_SA_EMAIL
    # When
    result = channel_factory._get_jwt_token_url(client_service_account_email)
    # Then
    assert client_service_account_email in result
    assert 'iamcredentials.googleapis.com' in result
    assert ':generateIdToken' in result


@pytest.mark.parametrize(
    'args',
    [
        [None, None],  # missing all
        [None, _TEST_SA_EMAIL],  # missing audience
        [_TEST_URL_HTTPS, None],  # missing service account
    ],
)
def test__get_jwt_token_nok_missing_arguments(args: List[str]):
    # Given/When
    result = channel_factory._get_jwt_token(*args)
    # Then
    assert result is None


@pytest.mark.parametrize(
    'secret_fqn',
    [
        None,
        '',
        'DOES_NOT_START_WITH_project_TOKEN/secrets/SECRET_NAME/versions/SECRET_VERSION',
        'projects/DOES_NOT_HAVE_secrets_TOKEN/SECRET_NAME/versions/SECRET_VERSION',
        'projects/DOES_NOT_HAVE_versions_TOKEN/secrets/SECRET_NAME/SECRET_VERSION',
    ],
)
def test__get_secret_nok_wrong_format(secret_fqn: str):
    # Given/When/Then
    with pytest.raises(ValueError):
        channel_factory._get_secret(secret_fqn)


def test__get_secret_ok(monkeypatch: pytest.MonkeyPatch):
    # Given
    secret_fqn = _TEST_API_KEY_SECRET_FQN
    _patch_gcp_secret_client(monkeypatch, secret_fqn, _TEST_API_KEY)
    # When
    result = channel_factory._get_secret(secret_fqn)
    # Then
    assert result == _TEST_API_KEY


def _patch_gcp_secret_client(
    monkeypatch: pytest.MonkeyPatch,
    secret_fqn: str = _TEST_API_KEY_SECRET_FQN,
    secret_content: str = _TEST_API_KEY,
) -> None:
    def mocked_gcp_secret_client():
        return _MockedSecretManagerServiceClient(secret_fqn, secret_content)

    monkeypatch.setattr(channel_factory, '_gcp_secret_client', mocked_gcp_secret_client)


def test__get_api_key_nok_all_none():
    # Given
    api_key = None
    secret_fqn = None
    # When
    result = channel_factory._get_api_key(api_key, secret_fqn)
    # Then
    assert result is None


def test__get_api_key_ok_only_api_key():
    # Given
    api_key = _TEST_API_KEY
    api_key_secret_fqn = None
    # When
    result = channel_factory._get_api_key(api_key, api_key_secret_fqn)
    # Then
    assert result == api_key


def test__get_api_key_ok_only_api_key_secret_fqn(monkeypatch: pytest.MonkeyPatch):
    # Given
    api_key = None
    api_key_secret_fqn = _TEST_API_KEY_SECRET_FQN
    _patch_gcp_secret_client(monkeypatch, api_key_secret_fqn, _TEST_API_KEY)
    # When
    result = channel_factory._get_api_key(api_key, api_key_secret_fqn)
    # Then
    assert result == _TEST_API_KEY


def test__get_api_key_ok_api_key_path_has_priority(monkeypatch: pytest.MonkeyPatch):
    # Given
    api_key = _TEST_API_KEY
    api_key_secret_fqn = _TEST_API_KEY_SECRET_FQN
    expected = f'{api_key}_DIFFERENT'
    _patch_gcp_secret_client(monkeypatch, api_key_secret_fqn, expected)
    # When
    result = channel_factory._get_api_key(api_key, api_key_secret_fqn)
    # Then
    assert result == expected


def test__create_composite_channel_credentials_ok_no_args():
    # Given
    api_key = None
    jwt_token = None
    # When
    result = channel_factory._create_composite_channel_credentials(
        api_key=api_key, jwt_token=jwt_token
    )
    # Then
    assert _is_ssl_channel_credentials(result)


def _is_ssl_channel_credentials(value: Any) -> bool:
    result = isinstance(value, grpc.ChannelCredentials)
    if result:
        inner_creds = getattr(value, '_credentials')
        result = inner_creds is not None
        result = result and isinstance(inner_creds, grpc._cygrpc.SSLChannelCredentials)
    return result


def test__create_composite_channel_credentials_ok_api_key():
    # Given
    api_key = _TEST_API_KEY
    jwt_token = None
    # When
    result = channel_factory._create_composite_channel_credentials(
        api_key=api_key, jwt_token=jwt_token
    )
    # Then
    assert _is_composite_channel_credentials(result)
    call_creds = list(result._credentials._call_credentialses)
    assert len(call_creds) == 1
    # same as inner function name
    assert _validate_md_call_credentials(call_creds[0], _INNER_API_KEY_FN_NAME)


def _is_composite_channel_credentials(
    value: Any, expected_channel_credentials_type: Any = grpc._cygrpc.SSLChannelCredentials
) -> bool:
    result = isinstance(value, grpc.ChannelCredentials)
    if result:
        inner_creds = getattr(value, '_credentials')
        result = inner_creds is not None
        result = result and isinstance(inner_creds, grpc._cygrpc.CompositeChannelCredentials)
        if result:
            inner_channel_creds = getattr(inner_creds, '_channel_credentials')
            result = inner_channel_creds is not None
            result = result and isinstance(inner_channel_creds, expected_channel_credentials_type)
    return result


def _validate_md_call_credentials(value: Any, expected_name: bytes) -> bool:
    result = isinstance(value, grpc._cygrpc.MetadataPluginCallCredentials)
    name = getattr(value, '_name')
    return result and name == expected_name


def test__create_composite_channel_credentials_ok_jwt():
    # Given
    api_key = None
    jwt_token = _TEST_JWT
    # When
    result = channel_factory._create_composite_channel_credentials(
        api_key=api_key, jwt_token=jwt_token
    )
    # Then
    assert _is_composite_channel_credentials(result)
    call_creds = list(result._credentials._call_credentialses)
    assert len(call_creds) == 1
    # same as plugin name
    assert _validate_md_call_credentials(call_creds[0], _AUTH_METADATA_PLUGIN_NAME)


def test__create_composite_channel_credentials_ok_all():
    # Given
    api_key = _TEST_JWT
    jwt_token = _TEST_JWT
    # When
    result = channel_factory._create_composite_channel_credentials(
        api_key=api_key, jwt_token=jwt_token
    )
    # Then
    assert _is_composite_channel_credentials(result)
    call_creds = list(result._credentials._call_credentialses)
    assert len(call_creds) == 2
    # same as plugin name
    assert _validate_md_call_credentials(call_creds[0], _INNER_API_KEY_FN_NAME)
    assert _validate_md_call_credentials(call_creds[1], _AUTH_METADATA_PLUGIN_NAME)


def test__create_retry_options_ok():
    # Given
    pb2_module = test_pb2_module
    pb2_desc = getattr(test_pb2_module, channel_factory._PB2_DESCRIPTOR_ATTR_NAME)
    package = pb2_desc.package
    services_by_name = pb2_desc.services_by_name
    # When
    result = channel_factory._create_retry_options(pb2_module)
    # Then: name
    service_lst = result.get('name', [{}])
    assert len(service_lst) == len(services_by_name)
    for svc, name in zip(service_lst, services_by_name):
        assert svc.get('service') == f'{package}.{name}'
    # Then: retryPolicy
    retry_pol = result.get('retryPolicy', {})
    assert retry_pol.get('maxAttempts') == channel_factory._RETRY_MAX_ATTEMPTS
    assert retry_pol.get('initialBackoff') == f'{channel_factory._RETRY_INITIAL_BACKOFF_IN_SEC}s'
    assert retry_pol.get('maxBackoff') == f'{channel_factory._RETRY_MAX_BACKOFF_IN_SEC}s'
    assert retry_pol.get('backoffMultiplier') == channel_factory._RETRY_BACKOFF_MULTIPLIER
    assert retry_pol.get('retryableStatusCodes') == channel_factory._RETRY_RETRYABLE_STATUS_CODES


def test__create_channel_options_ok():
    # Given
    pb2_module = test_pb2_module
    # When
    result = channel_factory._create_channel_options(pb2_module)
    # Then
    assert result and len(result) == 1
    key, json_str = result[0]
    assert key == channel_factory._GRPC_CHANNEL_OPTIONS_GRPC_SERVICE_CONFIG
    assert 'name' in json_str and 'retryPolicy' in json_str


@pytest.mark.parametrize(
    'args',
    [
        [None, None],  # missing all
        [None, _TEST_HEALTH_URL_PATH],  # missing url
        [_TEST_ENDPOINT_URL, None],  # missing path
    ],
)
def test__check_url_health_ok_missing(args):
    # Given/When/Then
    assert channel_factory._check_url_health(*args)


def test__check_url_health_ok(
    requests_mock: requests_mock.Mocker,
):  # pylint: disable=function-redefined
    # Given
    full_url = f'{_TEST_ENDPOINT_URL}/{_TEST_CLEAN_HEALTH_URL_PATH}'
    requests_mock.get(full_url, status_code=200)
    # When/Then
    assert channel_factory._check_url_health(_TEST_ENDPOINT_URL, _TEST_HEALTH_URL_PATH)


def test__check_url_health_ok(
    requests_mock: requests_mock.Mocker,
):  # pylint: disable=function-redefined
    # Given
    full_url = f'{_TEST_ENDPOINT_URL}/{_TEST_CLEAN_HEALTH_URL_PATH}'
    requests_mock.get(full_url, status_code=400)
    # When/Then
    with pytest.raises(requests.HTTPError):
        channel_factory._check_url_health(_TEST_ENDPOINT_URL, _TEST_HEALTH_URL_PATH)


def test_create_grpc_channel_ok(
    monkeypatch: pytest.MonkeyPatch, requests_mock: requests_mock.Mocker
):  # pylint: disable=function-redefined
    # Given
    full_url = f'{_TEST_ENDPOINT_URL}/{_TEST_CLEAN_HEALTH_URL_PATH}'
    requests_mock.get(full_url, status_code=200)

    def mocked_get_jwt_token(*args, **kwargs):  # pylint: disable=unused-argument
        return _TEST_JWT

    monkeypatch.setattr(channel_factory, '_get_jwt_token', mocked_get_jwt_token)
    _patch_gcp_secret_client(monkeypatch)
    # When
    result = channel_factory.create_grpc_channel(
        service_url=f'{_TEST_ENDPOINT_URL}/some_path_to_ignored',
        pb2_module=test_pb2_module,
        health_url_path=_TEST_HEALTH_URL_PATH,
        api_key=_TEST_API_KEY,
        api_key_secret_fqn=_TEST_API_KEY_SECRET_FQN,
        client_service_account_email=_TEST_SA_EMAIL,
    )
    assert isinstance(result, grpc.Channel)

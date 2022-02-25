# vim: ai:sw=4:ts=4:sta:et:fo=croql
"""
Generic `gRPC`_ (`PyPI`_) `Channel`_ handling.py

.. _gRPC: https://grpc.io/
.. _PyPI: https://pypi.org/project/grpcio/
.. _Channel: https://grpc.github.io/grpc/python/grpc_channelz.html
"""

import json
import logging
import types
from typing import Any, Dict, List, Optional
import urllib

import cachetools

import google.auth
import google.auth.impersonated_credentials
import google.auth.jwt
import google.auth.transport.grpc  # must be before you import requests
import google.auth.transport.requests  # must be before you import requests
import google.cloud.secretmanager
import google.oauth2.service_account
import grpc
import requests


_LOGGER: logging.Logger = logging.getLogger(__name__)

_PB2_DESCRIPTOR_ATTR_NAME: str = 'DESCRIPTOR'
_GRPC_CHANNEL_OPTIONS_GRPC_SERVICE_CONFIG: str = 'grpc.service_config'

_RETRY_MAX_ATTEMPTS: int = 5
_RETRY_INITIAL_BACKOFF_IN_SEC: float = 1
_RETRY_MAX_BACKOFF_IN_SEC: float = 5
_RETRY_BACKOFF_MULTIPLIER: float = 1.5
_RETRY_RETRYABLE_STATUS_CODES: List[str] = ['RESOURCE_EXHAUSTED', 'UNAVAILABLE']

_MAX_GRPC_RESPONSE_DEADLINE_IN_SEC: int = 24 * 60 * 60  # 1 day
_MAX_GRPC_RESPONSE_SIZE_IN_BYTES: int = 100 * 1024 * 1024  # 100MB

_API_KEY_MD_KEY_NAME: str = 'x-api-key'
_X_GOOGLE_BACKEND_MD_KEY_NAME: str = 'x-google-backend'
_X_GOOGLE_BACKEND_MD_DEADLINE_ENTRY_TMPL: str = 'deadline {}'

_GCP_SECRET_NAME_TMPL: str = 'projects/{project_id}/secrets/{secret_id}/versions/{version}'
_DEFAULT_GCP_SECRET_VERSION: str = 'latest'

_JWT_DEFAULT_CREDS_SCOPE: str = 'https://www.googleapis.com/auth/cloud-platform'
_JWT_DEFAULT_LIFETIME_IN_SEC: int = 2 * 60  # 2 minutes
_JWT_SA_CREDENTIALS_URL_TMPL: str = (
    'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{}:generateIdToken'
)
_JWT_SA_CREDENTIALS_HEADERS: Dict[str, str] = {'Content-Type': 'application/json'}


def create_grpc_channel(
    *,
    service_url: str,
    pb2_module: types.ModuleType,
    health_url_path: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_secret_fqn: Optional[str] = None,
    client_service_account_email: Optional[str] = None,
) -> grpc.Channel:
    """
    Creates an instance of :py:class:`grpc.Channel` with
    standard validation, retry, and exponential backoff.

    Proper usage::

        service_url = 'https://example.com:8443'
        # if you need extra API key
        api_key = 'YOUR_API_KEY_VALUE'
        # if you need to impersonate a service account
        client_service_account_email = 'CLIENT_SA@PROJECT_ID.iam.gserviceaccount.com'
        with create_grpc_channel(
            service_url=service_url,
            pb2_module=MyService_pb2,
            api_key=api_key,
            client_service_account_email=client_service_account_email
        ) as channel:
            # service stub
            stub = my_service.MyServiceStub(channel)
            # build call request
            request = my_service.MyServiceApiFunctionRequest()
            # API call
            response = stub.MyServiceApiFunction(request)

    Args:
        service_url: gRPC server url, to which the channel is to be created.
            Examples: ``http://localhost:5000`` or ``https://my-service.example.com``.
        pb2_module: your proto generated Python module ``_pb2``.
            It will infer namespace and API from it, to set call options.
        health_url_path: (optional) URL path to check if the server is alive.
        api_key: (optional) which API key value to add to request header.
        api_key_secret_fqn: (optional) a GCP Secret Manager secret full-qualified name
            from where to retrieve the API key (has priority over ``api_key``).
            It must be the something like below, where `SECRET_VERSION` is usually `latest`
                `projects/GCP_PROJECT_ID/secrets/SECRET_NAME/versions/SECRET_VERSION`
        client_service_account_email: (optional) which Service Account to impersonate,
            it requires the ``endpoint_url`` argument.

    Returns:
        A configured instance of :py:class:`grpc.Channel`.
    """
    _LOGGER.debug('Creating <%s> with <%s>.', grpc.Channel.__name__, locals())
    # e.g.: service_url = 'https://example.com:8443/some_path'
    if not service_url:
        raise RuntimeError(
            f'Endpoint URL argument is required. Got: <{service_url}>({type(service_url)}).'
        )
    # validate required module
    if not isinstance(pb2_module, (types.ModuleType,)) or not hasattr(
        pb2_module, _PB2_DESCRIPTOR_ATTR_NAME
    ):
        raise RuntimeError(
            'PB2 module argument must be the protobuf generated "_pb2" module reference. '
            f'Got: <{pb2_module}>({type(pb2_module)})'
        )
    parsed_url = urllib.parse.urlparse(service_url)
    # gRPC endpoint does NOT have the protocol
    grpc_endpoint = f'{parsed_url.hostname}'
    if parsed_url.port:
        grpc_endpoint = f'{grpc_endpoint}:{parsed_url.port}'
    # authorization needs protocol but NOT port
    endpoint_to_authenticate = f'{parsed_url.scheme}://{parsed_url.hostname}'
    # for health check you need full url without any extra path
    _check_url_health(f'{parsed_url.scheme}://{grpc_endpoint}', health_url_path)
    _LOGGER.debug(
        'gRPC endpoint <%s> being authenticated in GCP as <%s>.',
        grpc_endpoint,
        endpoint_to_authenticate,
    )
    # channel options, like exponential backoff
    options: Dict[str:Any] = _create_channel_options(pb2_module)
    # a secure channel only makes sense if it is a TLS endpoint
    if parsed_url.scheme == 'https':
        _LOGGER.debug('Endpoint <%s> requires authorization and authentication.', grpc_endpoint)
        channel_credentials = _create_channel_credentials(
            endpoint_to_authenticate=endpoint_to_authenticate,
            api_key=api_key,
            api_key_secret_fqn=api_key_secret_fqn,
            client_service_account_email=client_service_account_email,
        )
        result = grpc.secure_channel(
            target=grpc_endpoint, credentials=channel_credentials, options=options
        )
    else:
        _LOGGER.debug(
            'Endpoint <%s> does NOT requires authorization and authentication. '
            'Skipping all authorization.',
            grpc_endpoint,
        )
        # Ignores all authentication
        result = grpc.insecure_channel(target=grpc_endpoint, options=options)
    _LOGGER.info(
        'Created gRPC channel of type <%s> for endpoint <%s>.',
        result.__class__.__name__,
        grpc_endpoint,
    )
    return result


def _check_url_health(
    endpoint_url: Optional[str] = None,
    health_url_path: Optional[str] = None,
) -> bool:
    if endpoint_url and health_url_path:
        _LOGGER.debug(
            'Endpoint <%s> with health path <%s> to be checked', endpoint_url, health_url_path
        )
        response = requests.get(f'{endpoint_url}/{health_url_path.strip("/")}')
        response.raise_for_status()
    return True


def _create_channel_options(pb2_module: types.ModuleType) -> Dict[str, Any]:
    """
    From: https://grpc.github.io/grpc/python/glossary.html#term-channel_arguments
    See: https://fuchsia.googlesource.com/third_party/grpc/+/HEAD/doc/service_config.md
    See: https://stackoverflow.com/questions/64227270/use-retrypolicy-with-python-grpc-client
    """
    retry_options = _create_retry_options(pb2_module)
    return [(_GRPC_CHANNEL_OPTIONS_GRPC_SERVICE_CONFIG, json.dumps(retry_options))]


def _create_retry_options(pb2_module: types.ModuleType) -> Dict[str, Any]:
    """
    See: https://stackoverflow.com/questions/64227270/use-retrypolicy-with-python-grpc-client
    """
    result = {}
    # gRPC options a specific to each service, collecting all
    service_lst: List[Dict[str, str]] = []
    module_desc = getattr(pb2_module, _PB2_DESCRIPTOR_ATTR_NAME, None)
    if not module_desc:
        raise RuntimeError(
            f'Module {pb2_module}({type(pb2_module)}) has no {_PB2_DESCRIPTOR_ATTR_NAME} attribute.'
        )
    proto_package = module_desc.package
    for service_name in module_desc.services_by_name:
        service_lst.append({'service': f'{proto_package}.{service_name}'})
    result['name'] = service_lst
    # exponential backoff retry policy
    result['retryPolicy'] = {
        'maxAttempts': _RETRY_MAX_ATTEMPTS,
        'initialBackoff': f'{_RETRY_INITIAL_BACKOFF_IN_SEC}s',
        'maxBackoff': f'{_RETRY_MAX_BACKOFF_IN_SEC}s',
        'backoffMultiplier': _RETRY_BACKOFF_MULTIPLIER,
        'retryableStatusCodes': _RETRY_RETRYABLE_STATUS_CODES,
    }
    _LOGGER.debug('Created retry options <%s>.', result)
    return result


def _create_channel_credentials(
    *,
    endpoint_to_authenticate: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_secret_fqn: Optional[str] = None,
    client_service_account_email: Optional[str] = None,
) -> grpc.ChannelCredentials:
    # API key
    api_key = _get_api_key(api_key, api_key_secret_fqn)
    # JWT token
    jwt_token = _get_jwt_token(
        audience=endpoint_to_authenticate, client_service_account_email=client_service_account_email
    )
    # Auth metadata plugin
    result = _create_composite_channel_credentials(api_key=api_key, jwt_token=jwt_token)
    return result


def _get_api_key(api_key: Optional[str] = None, api_key_secret_fqn: Optional[str] = None):
    if api_key_secret_fqn:
        _LOGGER.debug('Extracting API key from secret <%s>.', api_key_secret_fqn)
        api_key = _get_secret(api_key_secret_fqn)
    if api_key:
        _LOGGER.debug('API key starts with <%s> and ends with <%s>.', api_key[:5], api_key[-5:])
    else:
        _LOGGER.debug('NO API key found.')
    return api_key


def _get_secret(secret_fqn: str) -> str:
    """
    Retrieves a secret, by fully-qualified name.
    Args:
        secret_fqn (str): which secret to retrieve.
            It must be the something like below, where `SECRET_VERSION` is usually `latest`:
                `projects/GCP_PROJECT_ID/secrets/SECRET_NAME/versions/SECRET_VERSION`
    Returns:
        Secret content
    """
    client = _gcp_secret_client()
    logging.info('Retrieving secret <%s>', secret_fqn)
    if (
        not secret_fqn
        or not secret_fqn.startswith('projects/')
        or '/secrets/' not in secret_fqn
        or '/versions/' not in secret_fqn
    ):
        raise ValueError(
            'Secret fully qualified name must be in the form '
            '"projects/GCP_PROJECT_ID/secrets/SECRET_NAME/versions/SECRET_VERSION". '
            f'Got: <{secret_fqn}>'
        )
    try:
        response = client.access_secret_version(request={'name': secret_fqn})
    except Exception as err:
        msg = f'Could not retrieve secret <{secret_fqn}>. Error: {err}'
        logging.critical(msg)
        raise RuntimeError(msg) from err
    return response.payload.data.decode('UTF-8')


@cachetools.cached(cache=cachetools.LRUCache(maxsize=1))
def _gcp_secret_client() -> google.cloud.secretmanager.SecretManagerServiceClient:
    return google.cloud.secretmanager.SecretManagerServiceClient()


def _get_jwt_token(
    audience: Optional[str] = None, client_service_account_email: Optional[str] = None
) -> str:
    # pylint: disable=line-too-long
    # Full documentation on API: https://cloud.google.com/iam/docs/reference/credentials/rest/v1/projects.serviceAccounts/generateIdToken
    # pylint: enable=line-too-long
    result = None
    if audience and client_service_account_email:
        _LOGGER.debug(
            'Retrieving JWT for audience <%s> and service account <%s>.',
            audience,
            client_service_account_email,
        )
        # pylint: disable=line-too-long
        # Source: https://medium.com/google-cloud/service-account-credentials-api-a-solution-to-different-issues-dc7434037115
        # pylint: enable=line-too-long
        default_cred, _ = google.auth.default(scopes=[_JWT_DEFAULT_CREDS_SCOPE])
        _LOGGER.debug(
            'Got default credentials for scope <%s> and principal email <%s>.',
            getattr(default_cred, 'scopes', None),
            getattr(default_cred, 'email', None),
        )
        auth_session = google.auth.transport.requests.AuthorizedSession(default_cred)
        body = json.dumps({'audience': audience})
        url = _get_jwt_token_url(client_service_account_email)
        token_response = auth_session.post(url, data=body, headers=_JWT_SA_CREDENTIALS_HEADERS)
        jwt = token_response.json()
        result = jwt.get('token')

        _LOGGER.debug(
            'Got authentication session response with keys <%s> '
            'and token starting with <%s> and ending with <%s>.',
            list(jwt.keys()),
            result[:5],
            result[-5:],
        )
    return result


def _get_jwt_token_url(client_service_account_email: str) -> str:
    return _JWT_SA_CREDENTIALS_URL_TMPL.format(client_service_account_email)


def _create_composite_channel_credentials(
    *,
    api_key: Optional[str] = None,
    jwt_token: Optional[str] = None,
) -> grpc.ChannelCredentials:
    channel_credentials = google.auth.transport.grpc.SslCredentials().ssl_credentials
    call_credentials: List[grpc.CallCredentials] = []
    # API key
    if api_key:

        def _api_key_fn(_, callback):
            callback(
                [
                    (
                        _API_KEY_MD_KEY_NAME,
                        api_key,
                    )
                ],
                None,
            )

        _LOGGER.debug(
            'Adding API key call credentials plugin to channel credential. '
            'API key starts with <%s> and ends with <%s>.',
            api_key[:5],
            api_key[-5:],
        )
        call_credentials.append(grpc.metadata_call_credentials(_api_key_fn))
    # JWT: service account to generate token to targeted endpoint
    if jwt_token:
        _LOGGER.debug(
            'Adding JWT call credentials plugin to channel credential. '
            'JWT starts with <%s> and ends with <%s>.',
            jwt_token[:5],
            jwt_token[-5:],
        )
        call_credentials.append(grpc.access_token_call_credentials(jwt_token))
    # result
    if len(call_credentials) > 1:  # multiple call credentials
        result = grpc.composite_channel_credentials(channel_credentials, *call_credentials)
    elif len(call_credentials) == 1:  # just one
        result = grpc.composite_channel_credentials(channel_credentials, call_credentials[0])
    else:  # no call credentials
        result = channel_credentials
    return result

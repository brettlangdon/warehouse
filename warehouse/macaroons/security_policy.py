# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64

from pyramid.interfaces import IAuthorizationPolicy, ISecurityPolicy
from pyramid.threadlocal import get_current_request
from zope.interface import implementer

from warehouse.cache.http import add_vary_callback
from warehouse.errors import WarehouseDenied
from warehouse.macaroons import InvalidMacaroonError
from warehouse.macaroons.interfaces import IMacaroonService
from warehouse.utils.security_policy import AuthenticationMethod


def _extract_basic_macaroon(auth):
    """
    A helper function for extracting a macaroon from a
    HTTP Basic Authentication-style header.

    Returns None if the header doesn't contain a structurally
    valid macaroon, or the candidate (not yet verified) macaroon
    in a serialized form.
    """
    try:
        authorization = base64.b64decode(auth).decode()
        auth_method, _, auth = authorization.partition(":")
    except ValueError:
        return None

    if auth_method != "__token__":
        return None

    # Strip leading/trailing whitespace characters from the macaroon
    auth = auth.strip()

    return auth


def _extract_http_macaroon(request):
    """
    A helper function for the extraction of HTTP Macaroon from a given request.
    Returns either a None if no macaroon could be found, or the string
    that represents our serialized macaroon.
    """
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None

    try:
        auth_method, auth = authorization.split(" ", 1)
    except ValueError:
        return None

    if auth_method.lower() == "basic":
        return _extract_basic_macaroon(auth)
    elif auth_method.lower() == "token":
        return auth

    return None


@implementer(ISecurityPolicy)
class MacaroonSecurityPolicy:
    def identity(self, request):
        # If we're calling into this API on a request, then we want to register
        # a callback which will ensure that the response varies based on the
        # Authorization header.
        request.add_response_callback(add_vary_callback("Authorization"))
        request.authentication_method = AuthenticationMethod.MACAROON

        # We need to extract our Macaroon from the request.
        macaroon = _extract_http_macaroon(request)
        if macaroon is None:
            return None

        # Check to see if our Macaroon exists in the database, and if so
        # fetch the user that is associated with it.
        macaroon_service = request.find_service(IMacaroonService, context=None)

        try:
            dm = macaroon_service.find_from_raw(macaroon)
        except InvalidMacaroonError:
            return None

        # Every Macaroon is either associated with a user or an OIDC provider.
        if dm.user is not None:
            return dm.user
        return dm.oidc_provider

    def remember(self, request, userid, **kw):
        # This is a NO-OP because our Macaroon header policy doesn't allow
        # the ability for authentication to "remember" the user id. This
        # assumes it has been configured in clients somewhere out of band.
        return []

    def forget(self, request, **kw):
        # This is a NO-OP because our Macaroon header policy doesn't allow
        # the ability for authentication to "forget" the user id. This
        # assumes it has been configured in clients somewhere out of band.
        return []

    def authenticated_userid(self, request):
        # Handled by MultiSecurityPolicy
        return NotImplemented

    def permits(self, request, context, permission):
        # Handled by MultiSecurityPolicy
        return NotImplemented


@implementer(IAuthorizationPolicy)
class MacaroonAuthorizationPolicy:
    def __init__(self, policy):
        self.policy = policy

    def permits(self, context, principals, permission):
        # The Pyramid API doesn't let us access the request here, so we have to pull it
        # out of the thread local instead.
        # TODO: Work with Pyramid devs to figure out if there is a better way to support
        #       the worklow we are using here or not.
        request = get_current_request()

        # Our request could possibly be a None, if there isn't an active request, in
        # that case we're going to always deny, because without a request, we can't
        # determine if this request is authorized or not.
        if request is None:
            return WarehouseDenied(
                "There was no active request.", reason="no_active_request"
            )

        # Re-extract our Macaroon from the request, it sucks to have to do this work
        # twice, but I believe it is inevitable unless we pass the Macaroon back as
        # a principal-- which doesn't seem to be the right fit for it.
        macaroon = _extract_http_macaroon(request)

        # This logic will only happen on requests that are being authenticated with
        # Macaroons. Any other request will just fall back to the standard Authorization
        # policy.
        if macaroon is not None:
            valid_permissions = ["upload"]
            macaroon_service = request.find_service(IMacaroonService, context=None)

            try:
                macaroon_service.verify(macaroon, request, context, permission)
            except InvalidMacaroonError as exc:
                return WarehouseDenied(
                    f"Invalid API Token: {exc}", reason="invalid_api_token"
                )

            # If our Macaroon is verified, and for a valid permission then we'll pass
            # this request to our underlying Authorization policy, so it can handle its
            # own authorization logic on the principal.
            if permission in valid_permissions:
                return self.policy.permits(context, principals, permission)
            else:
                return WarehouseDenied(
                    f"API tokens are not valid for permission: {permission}!",
                    reason="invalid_permission",
                )

        else:
            return self.policy.permits(context, principals, permission)

    def principals_allowed_by_permission(self, context, permission):
        # We just dispatch this, because Macaroons don't restrict what principals are
        # allowed by a particular permission, they just restrict specific requests
        # to not have that permission.
        return self.policy.principals_allowed_by_permission(context, permission)

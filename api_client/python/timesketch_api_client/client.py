# Copyright 2017 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Timesketch API client."""
from __future__ import unicode_literals

import time
import os
import logging
import sys

# pylint: disable=wrong-import-order
import bs4
import requests

# pylint: disable=redefined-builtin
from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import InsecureRequestWarning
import webbrowser

# pylint: disable-msg=import-error
from google_auth_oauthlib import flow as googleauth_flow
import google.auth.transport.requests
import pandas

from . import credentials
from . import definitions
from . import error
from . import index
from . import sketch
from . import user
from . import version
from . import sigma


logger = logging.getLogger("timesketch_api.client")


class TimesketchApi:
    """Timesketch API object

    Attributes:
        api_root: The full URL to the server API endpoint.
        session: Authenticated HTTP session.
    """

    DEFAULT_OAUTH_SCOPE = [
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]

    DEFAULT_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    DEFAULT_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    DEFAULT_OAUTH_PROVIDER_URL = "https://www.googleapis.com/oauth2/v1/certs"
    DEFAULT_OAUTH_LOCALHOST_URL = "http://localhost"
    DEFAULT_OAUTH_API_CALLBACK = "/login/api_callback/"

    # Default retry count for operations that attempt a retry.
    DEFAULT_RETRY_COUNT = 5

    def __init__(
        self,
        host_uri,
        username,
        password="",
        verify=True,
        client_id="",
        client_secret="",
        auth_mode="userpass",
        create_session=True,
    ):
        """Initializes the TimesketchApi object.

        Args:
            host_uri: URI to the Timesketch server (https://<server>/).
            username: User username.
            password: User password.
            verify: Verify server SSL certificate.
            client_id: The client ID if OAUTH auth is used.
            client_secret: The OAUTH client secret if OAUTH is used.
            auth_mode: The authentication mode to use. Defaults to 'userpass'
                Supported values are 'userpass' (username/password combo),
                'http-basic' (HTTP Basic authentication) and oauth.
            create_session: Boolean indicating whether the client object
                should create a session object. If set to False the
                function "set_session" needs to be called before proceeding.

        Raises:
            ConnectionError: If the Timesketch server is unreachable.
            RuntimeError: If the client is unable to authenticate to the
                backend.
        """
        self._host_uri = host_uri
        self.api_root = "{0:s}/api/v1".format(host_uri)
        self.credentials = None
        self._flow = None

        if not create_session:
            # Session needs to be set manually later using set_session()
            self._session = None
            return

        try:
            self._session = self._create_session(
                username,
                password,
                verify=verify,
                client_id=client_id,
                client_secret=client_secret,
                auth_mode=auth_mode,
            )
        except ConnectionError as exc:
            raise ConnectionError("Timesketch server unreachable") from exc
        except RuntimeError as e:
            raise RuntimeError(
                "Unable to connect to server, error: {0!s}".format(e)
            ) from e

    @property
    def current_user(self):
        """Property that returns the user object of the logged in user."""
        return user.User(self)

    @property
    def version(self):
        """Property that returns back the API client version."""
        version_dict = self.fetch_resource_data("version/")
        ts_version = None
        if version_dict:
            ts_version = version_dict.get("meta", {}).get("version")

        if ts_version:
            return "API Client: {0:s}\nTS Backend: {1:s}".format(
                version.get_version(), ts_version
            )

        return "API Client: {0:s}".format(version.get_version())

    @property
    def session(self):
        """Property that returns the session object."""
        if self._session is None:
            raise ValueError("Session is not set.")
        return self._session

    def set_credentials(self, credential_object):
        """Sets the credential object."""
        self.credentials = credential_object

    def set_session(self, session_object):
        """Sets the session object."""
        self._session = session_object

    def _authenticate_session(self, session, username, password):
        """Post username/password to authenticate the HTTP session.

        Args:
            session: Instance of requests.Session.
            username: User username.
            password: User password.
        """
        # Do a POST to the login handler to set up the session cookies
        data = {"username": username, "password": password}
        session.post("{0:s}/login/".format(self._host_uri), data=data)

    def _set_csrf_token(self, session):
        """Retrieve CSRF token from the server and append to HTTP headers.

        Args:
            session: Instance of requests.Session.
        """
        # Scrape the CSRF token from the response
        response = session.get(self._host_uri)
        soup = bs4.BeautifulSoup(response.text, features="html.parser")

        tag = soup.find(id="csrf_token")
        csrf_token = None
        if tag:
            csrf_token = tag.get("value")
        else:
            tag = soup.find("meta", attrs={"name": "csrf-token"})
            if tag:
                csrf_token = tag.attrs.get("content")

        if not csrf_token:
            return

        session.headers.update({"x-csrftoken": csrf_token, "referer": self._host_uri})

    def _create_oauth_session(
        self,
        client_id="",
        client_secret="",
        client_secrets_file=None,
        host="localhost",
        port=8080,
        open_browser=False,
        run_server=True,
        skip_open=False,
    ):
        """Return an OAuth session.

        Args:
            client_id (str): The client ID if OAUTH auth is used.
            client_secret (str): The OAUTH client secret if OAUTH is used.
            client_secrets_file (str): Path to the JSON file that contains the client
                secrets, in the client_secrets format.
            host (str): Host address the OAUTH web server will bind to.
            port (int): Port the OAUTH web server will bind to.
            open_browser (bool): A boolean, if set to false (default) a browser window
                will not be automatically opened.
            run_server (bool): A boolean, if set to true (default) a web server is
                run to catch the OAUTH request and response.
            skip_open (bool): A booelan, if set to True (defaults to False) an
                authorization URL is printed on the screen to visit. This is
                only valid if run_server is set to False.

        Return:
            session: Instance of requests.Session.

        Raises:
            RuntimeError: if unable to log in to the application.
        """
        if client_secrets_file:
            if not os.path.isfile(client_secrets_file):
                raise RuntimeError(
                    "Unable to log in, client secret files does not exist."
                )
            flow = googleauth_flow.InstalledAppFlow.from_client_secrets_file(
                client_secrets_file,
                scopes=self.DEFAULT_OAUTH_SCOPE,
                autogenerate_code_verifier=True,
            )
        else:
            provider_url = self.DEFAULT_OAUTH_PROVIDER_URL
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": self.DEFAULT_OAUTH_AUTH_URL,
                    "token_uri": self.DEFAULT_OAUTH_TOKEN_URL,
                    "auth_provider_x509_cert_url": provider_url,
                    "redirect_uris": [self.DEFAULT_OAUTH_LOCALHOST_URL],
                },
            }

            flow = googleauth_flow.InstalledAppFlow.from_client_config(
                client_config,
                self.DEFAULT_OAUTH_SCOPE,
                autogenerate_code_verifier=True,
            )

            flow.redirect_uri = self.DEFAULT_OAUTH_LOCALHOST_URL

        if run_server:
            _ = flow.run_local_server(host=host, port=port, open_browser=open_browser)
        else:
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                msg = (
                    "You will be asked to paste a token into this session to"
                    "authenticate, but the session doesn't have a tty"
                )
                raise RuntimeError(msg)

            auth_url, _ = flow.authorization_url(prompt="select_account")

            if skip_open:
                print("Visit the following URL to authenticate: {0:s}".format(auth_url))
            else:
                open_browser = input("Open the URL in a browser window? [y/N] ")
                if open_browser.lower() == "y" or open_browser.lower() == "yes":
                    webbrowser.open(auth_url)
                else:
                    print(
                        "Need to manually visit URL to authenticate: "
                        "{0:s}".format(auth_url)
                    )

            code = input("Enter the token code: ")
            _ = flow.fetch_token(code=code)

        session = flow.authorized_session()
        self._flow = flow
        self.credentials = credentials.TimesketchOAuthCredentials()
        self.credentials.credential = flow.credentials
        return self.authenticate_oauth_session(session)

    def authenticate_oauth_session(self, session):
        """Authenticate an OAUTH session.

        Args:
            session: Authorized session object.
        """
        # Authenticate to the Timesketch backend.
        login_callback_url = "{0:s}{1:s}".format(
            self._host_uri, self.DEFAULT_OAUTH_API_CALLBACK
        )
        params = {
            "id_token": session.credentials.id_token,
        }
        response = session.get(login_callback_url, params=params)
        if response.status_code not in definitions.HTTP_STATUS_CODE_20X:
            error.error_message(
                response, message="Unable to authenticate", error=RuntimeError
            )

        self._set_csrf_token(session)
        return session

    def _create_session(
        self, username, password, verify, client_id, client_secret, auth_mode
    ):
        """Create authenticated HTTP session for server communication.

        Args:
            username (str): User to authenticate as.
            password (str): User password.
            verify (bool): Verify server SSL certificate.
            client_id (str): The client ID if OAUTH auth is used.
            client_secret (str): The OAUTH client secret if OAUTH is used.
            auth_mode (str): The authentication mode to use. Supported values are
                'userpass' (username/password combo), 'http-basic'
                (HTTP Basic authentication) and oauth

        Returns:
            Instance of requests.Session.
        """
        if auth_mode == "oauth":
            return self._create_oauth_session(client_id, client_secret)

        if auth_mode == "oauth_local":
            return self._create_oauth_session(
                client_id=client_id,
                client_secret=client_secret,
                run_server=False,
                skip_open=True,
            )

        session = requests.Session()

        # If using HTTP Basic auth, add the user/pass to the session
        if auth_mode == "http-basic":
            session.auth = (username, password)

        # SSL Cert verification is turned on by default.
        if not verify:
            session.verify = False
            # disable warnings, since user actively decided to set verify to false
            requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

        # Get and set CSRF token and authenticate the session if appropriate.
        self._set_csrf_token(session)
        if auth_mode == "userpass":
            self._authenticate_session(session, username, password)

        return session

    def fetch_resource_data(self, resource_uri, params=None):
        """Makes an HTTP GET request to the specified resource URI with retries.

        This method attempts to fetch data from the Timesketch API. It implements
        a manual retry mechanism with a fixed 1-second backoff between attempts
        if the initial request fails or returns an empty (but valid JSON) response.
        Retries occur for network connection errors, API errors (non-20x status codes),
        JSON decoding errors, or if the API returns a successful (20x) response
        with a "falsy" JSON payload (e.g., null, empty list/dictionary).


        Args:
            resource_uri (str): The URI to the resource to be fetched.
            params (dict, optional): A dictionary of URL parameters to send
                in the GET request. Defaults to None.

        Returns:
            dict: A dictionary containing the JSON response data from the API.

        Raises:
            requests.exceptions.ConnectionError: If a connection error persists
                after all retry attempts.
            ValueError: If the API response cannot be JSON-decoded after all
                retry attempts.
            RuntimeError: If the API server returns an error (non-20x status code)
                or a "falsy" JSON response (e.g., null, empty list/dict)
                after all retry attempts.
        """
        resource_url = "{0:s}/{1:s}".format(self.api_root, resource_uri)

        # Start attempt count from 0, first loop with set it to 1
        attempt = 0
        result = None
        while True:
            attempt += 1
            # If this is not the first attempt, wait before trying again
            if attempt > 1:
                backoff_time = 0.5 * (2 ** (attempt - 2))
                logger.info(
                    "Waiting %.1fs before next attempt for request '%s'.",
                    backoff_time,
                    resource_url,
                )
                time.sleep(backoff_time)

            try:
                response = self.session.get(resource_url, params=params)
                result = error.get_response_json(response, logger)
                if result:
                    return result
            except RuntimeError as e:
                if attempt >= self.DEFAULT_RETRY_COUNT:
                    # Re-raise the original error after exhausting retries
                    error_msg = f"Error for request '{resource_url}' - '{e!s}'"
                    raise RuntimeError(error_msg) from e

                logger.warning(
                    "[%d/%d] API error (RuntimeError) for request '%s' "
                    "failed. Error: %s. Trying again...",
                    attempt,
                    self.DEFAULT_RETRY_COUNT,
                    resource_url,
                    str(e),
                )
            except ValueError as e:
                if attempt >= self.DEFAULT_RETRY_COUNT:
                    # Re-raise the original error after exhausting retries
                    error_msg = (
                        f"Error parsing response for request '{resource_url}'"
                        f" - {e!s}"
                    )
                    raise ValueError(error_msg) from e

                logger.warning(
                    "[%d/%d] Parsing the JSON response for request '%s' "
                    "failed. Error: %s. Trying again...",
                    attempt,
                    self.DEFAULT_RETRY_COUNT,
                    resource_url,
                    e,
                )
            except ConnectionError as e:  # Explicitly catch connection errors
                if attempt >= self.DEFAULT_RETRY_COUNT:
                    # Re-raise the original error after exhausting retries
                    error_msg = (
                        f"Connection error for request '{resource_url}' "
                        f"after {self.DEFAULT_RETRY_COUNT} attempts: {e!s}"
                    )
                    raise ConnectionError(error_msg) from e

                logger.warning(
                    "[%d/%d] Connection error for request '%s': %s. Trying again...",
                    attempt,
                    self.DEFAULT_RETRY_COUNT,
                    resource_url,
                    e,
                )

            if attempt >= self.DEFAULT_RETRY_COUNT:
                error_msg = (
                    "Unable to fetch JSON resource data for request: '{0:s}'"
                    " - Response: '{1!s}'".format(resource_url, result)
                )
                raise RuntimeError(error_msg)

    def create_sketch(self, name, description=None):
        """Create a new sketch.

        This method attempts to create a new sketch on the Timesketch server.
        It implements a retry mechanism with exponential backoff if the initial
        request fails due to network issues, API errors, or unexpected
        response formats.

        Args:
            name (str): Name of the sketch. Cannot be empty.
            description (str): Optional description of the sketch. If not
                provided, the sketch name will be used as the description.

        Returns:
            Instance of a Sketch object representing the newly created sketch.

        Raises:
            ValueError: If the provided sketch name is empty, or if the API
                response cannot be JSON-decoded after all retry attempts.
            requests.exceptions.ConnectionError: If a connection error persists
                after all retry attempts.
            RuntimeError: If the API server returns an error (non-20x status code),
                or if the expected 'objects' structure with a sketch ID is not
                found in the API response after all retry attempts.
        """
        if not description:
            description = name

        if not name:
            raise ValueError("Sketch name cannot be empty")
        resource_url = "{0:s}/sketches/".format(self.api_root)
        form_data = {"name": name, "description": description}
        last_exception = None

        for attempt in range(self.DEFAULT_RETRY_COUNT):
            response = self.session.post(resource_url, json=form_data)
            try:
                response_dict = error.get_response_json(response, logger)
                objects = response_dict.get("objects")

                if (
                    objects
                    and isinstance(objects, list)
                    and len(objects) > 0
                    and isinstance(objects[0], dict)
                    and "id" in objects[0]
                ):
                    sketch_id = objects[0]["id"]
                    return self.get_sketch(sketch_id)

                # Handle cases where 'objects' is missing, not a list, empty,
                # or its first element is not a dict or lacks an 'id'.
                # This assumes a 2xx response, as get_response_json would
                # raise otherwise.
                log_message = (
                    "API for sketch creation returned an unexpected 'objects' "
                    "format or it was empty."
                )
                logger.warning(
                    "[%d/%d] %s Response: %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    log_message,
                    response_dict,
                )
                last_exception = RuntimeError(
                    f"{log_message} Response: {response_dict!s}"
                )

            except RequestException as e:
                logger.warning(
                    "[%d/%d] Request error creating sketch '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    name,
                    e,
                )
                last_exception = e
            except ValueError as e:  # JSON decoding error
                logger.warning(
                    "[%d/%d] JSON error creating sketch '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    name,
                    e,
                )
                last_exception = e
            except RuntimeError as e:  # Non-20x status
                logger.warning(
                    "[%d/%d] API error creating sketch '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    name,
                    e,
                )
                last_exception = e

            if attempt < self.DEFAULT_RETRY_COUNT - 1:
                backoff_time = 0.5 * (2**attempt)  # Exponential backoff
                logger.info(
                    "Waiting %.1fs before next attempt to create sketch '%s'.",
                    backoff_time,
                    name,
                )
                time.sleep(backoff_time)
            else:
                # All attempts failed
                error_message_detail = (
                    "All {0:d} attempts to create sketch '{1:s}' failed.".format(
                        self.DEFAULT_RETRY_COUNT, name
                    )
                )
                logger.error("%s Last error: %s", error_message_detail, last_exception)
                if last_exception:
                    raise RuntimeError(
                        f"{error_message_detail} Last error: {last_exception!s}"
                    ) from last_exception
                raise RuntimeError(error_message_detail)

        # Fallback, should ideally be unreachable.
        raise RuntimeError(
            "Failed to create sketch '{0:s}' after all retries "
            "(unexpected loop exit).".format(name)
        )

    def create_user(self, username, password):
        """Create a new user.

        Args:
            username (str): Name of the user
            password (str): Password of the user

        Returns:
            True if user created successfully.

        Raises:
            RuntimeError: If response does not contain an 'objects' key after
                DEFAULT_RETRY_COUNT attempts.
        """

        retry_count = 0
        objects = None
        while True:
            resource_url = "{0:s}/users/".format(self.api_root)
            form_data = {"username": username, "password": password}
            response = self.session.post(resource_url, json=form_data)
            response_dict = error.get_response_json(response, logger)
            objects = response_dict.get("objects")
            if objects:
                break
            retry_count += 1

            if retry_count >= self.DEFAULT_RETRY_COUNT:
                raise RuntimeError("Unable to create a new user.")

        return user.User(user_id=objects[0]["id"], api=self)

    def list_users(self):
        """Get a list of all users.

        Yields:
            User object instances.
        """
        response = self.fetch_resource_data("users/")

        for user_dict in response.get("objects", [])[0]:
            user_id = user_dict["id"]
            user_obj = user.User(user_id=user_id, api=self)
            yield user_obj

    def get_user(self, user_id):
        """Get a user.

        Args:
            user_id: Primary key ID of the user.

        Returns:
            Instance of a User object.
        """
        return user.User(user_id=user_id, api=self)

    def get_oauth_token_status(self):
        """Return a dict with OAuth token status, if one exists."""
        if not self.credentials:
            return {"status": "No stored credentials."}
        return {
            "expired": self.credentials.credential.expired,
            "expiry_time": self.credentials.credential.expiry.isoformat(),
        }

    def get_sketch(self, sketch_id):
        """Get a sketch.

        Args:
            sketch_id: Primary key ID of the sketch.

        Returns:
            Instance of a Sketch object.
        """
        return sketch.Sketch(sketch_id, api=self)

    def get_aggregator_info(self, name="", as_pandas=False):
        """Returns information about available aggregators.

        Args:
            name (str): String with the name of an aggregator. If the name is not
                provided, a list with all aggregators is returned.
            as_pandas (bool): Boolean indicating that the results will be returned
                as a Pandas DataFrame instead of a list of dicts.

        Returns:
            A list with dict objects with the information about aggregators,
            unless as_pandas is set, then the function returns a DataFrame
            object.
        """
        resource_uri = "aggregation/info/"

        if name:
            data = {"aggregator": name}
            resource_url = "{0:s}/{1:s}".format(self.api_root, resource_uri)
            response = self.session.post(resource_url, json=data)
            response_json = error.get_response_json(response, logger)
        else:
            response_json = self.fetch_resource_data(resource_uri)

        if not as_pandas:
            return response_json

        lines = []
        if isinstance(response_json, dict):
            response_json = [response_json]

        for line in response_json:
            line_dict = {
                "name": line.get("name", "N/A"),
                "description": line.get("description", "N/A"),
            }
            for field_index, field in enumerate(line.get("fields", [])):
                line_dict["field_{0:d}_name".format(field_index + 1)] = field.get(
                    "name"
                )
                line_dict["field_{0:d}_description".format(field_index + 1)] = (
                    field.get("description"))  # fmt: skip
            lines.append(line_dict)

        return pandas.DataFrame(lines)

    def list_sketches(self, per_page=50, scope="user", include_archived=True):
        """Get a list of all open sketches that the user has access to.

        Args:
            per_page (int): Number of items per page when paginating. Default is 50.
            scope (str): What scope to get sketches as. Default to user.
                user: sketches owned by the user
                recent: sketches that the user has actively searched in
                shared: Get sketches that can be accessed
                admin: Get all sketches if the user is an admin
                archived: get archived sketches
                search: pass additional search query
            include_archived (bool): If archived sketches should be returned.

        Yields:
            Sketch objects instances.
        """
        url_params = {
            "per_page": per_page,
            "scope": scope,
            "include_archived": include_archived,
        }
        # Start with the first page
        page = 1
        has_next_page = True

        while has_next_page:
            url_params["page"] = page
            response = self.fetch_resource_data("sketches/", params=url_params)
            meta = response.get("meta", {})

            page = meta.get("next_page")
            if not page:
                has_next_page = False

            for sketch_dict in response.get("objects", []):
                sketch_id = sketch_dict["id"]
                sketch_name = sketch_dict["name"]
                sketch_obj = sketch.Sketch(
                    sketch_id=sketch_id, api=self, sketch_name=sketch_name
                )
                yield sketch_obj

    def get_searchindex(self, searchindex_id):
        """Get a searchindex.

        Args:
            searchindex_id: Primary key ID of the searchindex.

        Returns:
            Instance of a SearchIndex object.
        """
        return index.SearchIndex(searchindex_id, api=self)

    def create_searchindex(self, searchindex_name: str, opensearch_index_name: str):
        """Create a new SearchIndex.

        This method attempts to create a new searchindex on the Timesketch server.
        It implements a retry mechanism with exponential backoff if the initial
        request fails due to network issues, API errors, or unexpected
        response formats.

        Args:
            searchindex_name: Name for the searchindex.
            opensearch_index_name: The name of the index in opensearch.

        Returns:
            Instance of a SearchIndex object.

        Raises:
            RuntimeError: If the SearchIndex fails to create after all retries,
                or if the API returns an unexpected response format.
            requests.exceptions.RequestException: If a connection error persists
                after all retries.
            ValueError: If the API response cannot be JSON-decoded after all retries.
        """
        resource_url = f"{self.api_root}/searchindices/"
        form_data = {
            "searchindex_name": searchindex_name,
            "es_index_name": opensearch_index_name,
        }
        last_exception = None

        for attempt in range(self.DEFAULT_RETRY_COUNT):
            try:
                response = self.session.post(resource_url, json=form_data)
                response_dict = error.get_response_json(
                    response, logger
                )  # Raises RuntimeError for non-20x, ValueError for JSON decode
                objects = response_dict.get("objects")

                if (
                    objects
                    and isinstance(objects, list)
                    and len(objects) > 0
                    and isinstance(objects[0], dict)
                    and "id" in objects[0]
                ):
                    searchindex_id = objects[0]["id"]
                    return index.SearchIndex(searchindex_id, api=self)

                log_message = (
                    "API for searchindex creation returned an unexpected 'objects' "
                    "format or it was empty."
                )
                logger.warning(
                    "[%d/%d] %s Response: %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    log_message,
                    response_dict,
                )
                last_exception = RuntimeError(
                    "{0:s} Response: {1!s}".format(log_message, response_dict)
                )

            except RequestException as e:
                logger.warning(
                    "[%d/%d] Request error creating searchindex '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    searchindex_name,
                    e,
                )
                last_exception = e
            except ValueError as e:  # JSON decoding error
                logger.warning(
                    "[%d/%d] JSON error creating searchindex '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    searchindex_name,
                    e,
                )
                last_exception = e
            except RuntimeError as e:  # Non-20x status or other API error
                logger.warning(
                    "[%d/%d] API error creating searchindex '%s': %s. Retrying...",
                    attempt + 1,
                    self.DEFAULT_RETRY_COUNT,
                    searchindex_name,
                    e,
                )
                last_exception = e

            if attempt < self.DEFAULT_RETRY_COUNT - 1:
                backoff_time = 0.5 * (2**attempt)  # Exponential backoff
                logger.info(
                    "Waiting %.1fs before next attempt to create searchindex '%s'.",
                    backoff_time,
                    searchindex_name,
                )
                time.sleep(backoff_time)
            else:
                # All attempts failed
                error_message_detail = (
                    "All {0:d} attempts to create searchindex '{1:s}' failed.".format(
                        self.DEFAULT_RETRY_COUNT, searchindex_name
                    )
                )
                logger.error("%s Last error: %s", error_message_detail, last_exception)
                if last_exception:
                    last_exception = RuntimeError(
                        f"{0:s} Response: {1!s}".format(log_message, response_dict)
                    )
                raise RuntimeError(error_message_detail)

        # Fallback, should ideally be unreachable.
        raise RuntimeError(
            "Failed to create searchindex '{0:s}' after all retries "
            "(unexpected loop exit).".format(searchindex_name)
        )

    def check_celery_status(self, job_id=""):
        """Return information about outstanding celery tasks or a specific one.

        Args:
            job_id (str): Optional Celery job identification string. If
                provided that specific job ID is queried, otherwise
                a check for all outstanding jobs is checked.

        Returns:
            A list of dict objects with the status of the celery task/tasks
            that were outstanding.
        """
        if job_id:
            response = self.fetch_resource_data("tasks/?job_id={0:s}".format(job_id))
        else:
            response = self.fetch_resource_data("tasks/")

        return response.get("objects", [])

    def list_searchindices(self):
        """Yields all searchindices that the user has access to.

        Yields:
            A SearchIndex object instances.
        """
        response = self.fetch_resource_data("searchindices/")
        response_objects = response.get("objects")
        if not response_objects:
            yield None
            return

        for index_dict in response_objects[0]:
            index_id = index_dict["id"]
            index_name = index_dict["name"]
            index_obj = index.SearchIndex(
                searchindex_id=index_id, api=self, searchindex_name=index_name
            )
            yield index_obj

    def refresh_oauth_token(self):
        """Refresh an OAUTH token if one is defined."""
        if not self.credentials:
            return
        request = google.auth.transport.requests.Request()
        self.credentials.credential.refresh(request)

    def list_sigmarules(self, as_pandas=False):
        """Fetches Sigma rules from the database.
        Fetches all Sigma rules stored in the database on the system
        and returns a list of SigmaRule objects of the rules.

        Args:
            as_pandas (bool): Boolean indicating that the results will be returned
                as a Pandas DataFrame instead of a list of SigmaRuleObjects.

        Returns:
            - List of Sigme rule object instances
            or
            - a pandas Dataframe with all rules if as_pandas is True.

        Raises:
            ValueError: If no rules are found.
        """
        rules = []
        response = self.fetch_resource_data("sigmarules/")

        if not response:
            raise ValueError("No rules found.")

        if as_pandas:
            return pandas.DataFrame.from_records(response.get("objects"))

        for rule_dict in response["objects"]:
            if not rule_dict:
                raise ValueError("No rules found.")

            index_obj = sigma.SigmaRule(api=self)
            for key, value in rule_dict.items():
                index_obj.set_value(key, value)
            rules.append(index_obj)
        return rules

    def create_sigmarule(self, rule_yaml):
        """Adds a single Sigma rule to the database.

        Adds a single Sigma rule to the database when `/sigmarules/` is called
        with a POST request.

        All attributes of the rule are taken by the `rule_yaml` value in the
        POST request.

        If no `rule_yaml` is found in the request, the method will fail as this
        is required to parse the rule.

        Args:
            rule_yaml (str): YAML of the Sigma Rule.

        Returns:
            Instance of a Sigma object.
        """

        retry_count = 0
        objects = None
        while True:
            resource_url = "{0:s}/sigmarules/".format(self.api_root)
            form_data = {"rule_yaml": rule_yaml}
            response = self.session.post(resource_url, json=form_data)
            response_dict = error.get_response_json(response, logger)
            objects = response_dict.get("objects")
            if objects:
                break
            retry_count += 1

            if retry_count >= self.DEFAULT_RETRY_COUNT:
                raise RuntimeError("Unable to create a new Sigma Rule.")

        rule_uuid = objects[0]["rule_uuid"]
        return self.get_sigmarule(rule_uuid)

    def get_sigmarule(self, rule_uuid):
        """Fetches a single Sigma rule from the database.
        Fetches a single Sigma rule selected by the `UUID`

        Args:
            rule_uuid: UUID of the Sigma rule.

        Returns:
            Instance of a SigmaRule object.
        """
        sigma_obj = sigma.SigmaRule(api=self)
        sigma_obj.from_rule_uuid(rule_uuid)

        return sigma_obj

    def parse_sigmarule_by_text(self, rule_text):
        """Obtain a parsed Sigma rule by providing text.

        Will parse a provided text `rule_yaml`, parse it and return as SigmaRule
        object.

        Args:
            rule_text: Full Sigma rule text.

        Returns:
            Instance of a Sigma object.

        Raises:
            ValueError: No Rule text given or issues parsing it.
        """
        if not rule_text:
            raise ValueError("No rule text given.")

        try:
            sigma_obj = sigma.Sigma(api=self)
            sigma_obj.from_text(rule_text)
        except ValueError:
            logger.error("Parsing Error, unable to parse the Sigma rule", exc_info=True)

        return sigma_obj  # pytype: disable=name-error  # py310-upgrade

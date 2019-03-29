"""Logic to interact with API."""

from datetime import datetime
from itertools import islice
from os import environ
from os.path import join
from urllib.parse import urljoin
import collections
import json
import os
import shutil
import subprocess
import time
import traceback
import uuid

from munch import Munch
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from six import iteritems
import click
import requests

from isabl_cli import utils
from isabl_cli.settings import import_from_string
from isabl_cli.settings import system_settings
from isabl_cli.settings import user_settings

requests.packages.urllib3.disable_warnings(  # pylint: disable=E1101
    InsecureRequestWarning
)


def isablfy(obj):
    """Convert objects to IsablDicts recursively."""
    if isinstance(obj, dict):
        if obj.get("model_name") == "Experiment":
            factory = Experiment
        elif obj.get("model_name") == "Analysis" or "wait_time" in obj:
            factory = Analysis
        elif obj.get("model_name") == "Assembly":
            factory = Assembly
        else:
            factory = IsablDict
        return factory((k, isablfy(v)) for k, v in iteritems(obj))
    elif isinstance(obj, (list, tuple)):
        return type(obj)(isablfy(v) for v in obj)
    else:
        return obj


class IsablDict(Munch):
    def __init__(self, *args, **kwargs):
        """If first argument is int or str, use it to retrieve an instance."""
        if len(args) == 1 and isinstance(args[0], (str, int)):
            super().__init__(isablfy(get_instance(self.api_endpoint, args[0])))
        else:
            super().__init__(*args, **kwargs)

    @classmethod
    def fromDict(cls, d):
        """Transform a dictionary into IsablDicts recursively."""
        return isablfy(d)

    def __dir__(self):
        """Bypass Munch's __dir__."""
        return super(dict, self).__dir__()  # pylint: disable=bad-super-call

    def __repr__(self):
        """Get a simple representation, Munch's is too long."""
        identifier = getattr(self, "system_id", getattr(self, "pk", None))
        return (
            f"{getattr(self, 'model_name', self.__class__.__name__)}({identifier})"
            if identifier
            else super().__repr__()
        )


class Analysis(IsablDict):

    api_endpoint = "analyses"

    def __repr__(self):
        """Get better representation for analyses."""
        identifier = getattr(self, "pk", None)
        application = getattr(self, "application", {})
        return (
            super().__repr__()
            if not identifier
            else (
                f"{application.get('name', 'Analysis')} "
                f"{application.get('version', 'No Version Available')}"
                f"({identifier})"
            )
        )


class Assembly(IsablDict):

    api_endpoint = "assemblies"

    def __repr__(self):
        """Get better representation for assemblies."""
        identifier = getattr(self, "name", getattr(self, "pk", None))
        return super().__repr__() if not identifier else f"Assembly({identifier})"


class Experiment(IsablDict):

    api_endpoint = "experiments"


def chunks(array, size):
    """Yield successive n-sized chunks from ``array``."""
    array = iter(array)
    return iter(lambda: tuple(islice(array, size)), ())


def get_api_url(url):
    """Get an API URL."""
    # hmm, don't like this messing around with slashes
    base_url = environ.get("ISABL_API_URL", "http://0.0.0.0:8000/api/v1/")
    base_url = base_url if base_url[-1] == "/" else f"{base_url}/"

    if not url.startswith(base_url):
        url = urljoin(base_url, url[1:] if url.startswith("/") else url)

    return url


def retry_request(method, **kwargs):
    """Retry request operation multiple times."""
    for i in [0.2, 1, 5, 60, 300]:  # attempt some retries
        response = getattr(requests, method)(verify=False, **kwargs)

        if not str(response.status_code).startswith("50"):
            break
        else:  # pragma: no cover
            click.secho(f"Request failed, retrying in {i}s...", fg="yellow", err=True)
            time.sleep(i)

    return response


def get_token_headers():
    """Get an API token and store it in user's home directory."""
    headers = {"Authorization": f"Token {user_settings.api_token}"}
    auth_url = get_api_url("/rest-auth/user/")
    response = retry_request("get", url=auth_url, headers=headers)

    try:
        assert "username" in response.json()
    except (json.JSONDecodeError, AssertionError):
        response = retry_request(
            "post",
            url=get_api_url("/rest-auth/login/"),
            data={
                "username": click.prompt(
                    "username",
                    type=str,
                    hide_input=False,
                    default="admin",
                    show_default=False,
                ),
                "password": click.prompt(
                    "password",
                    type=str,
                    hide_input=True,
                    default="admin",
                    show_default=False,
                ),
            },
        )

        if not response.ok and "non_field_errors" in response.text:
            click.secho("\n".join(response.json()["non_field_errors"]), fg="red")
            return get_token_headers()
        else:
            click.echo(f"Request Error: {response.url}")
            response.raise_for_status()

        user_settings.api_token = response.json()["key"]  # pylint: disable=invalid-name
        headers = {"Authorization": f"Token {user_settings.api_token}"}
        click.secho("Successful authorization! Token stored.", fg="green")

    return headers


def api_request(method, url, authenticate=True, **kwargs):
    """Perform any request operation using a naive retry implementation."""
    kwargs["params"] = kwargs.get("params", {})
    kwargs["params"]["format"] = "json"
    kwargs["url"] = get_api_url(url)

    if authenticate:
        kwargs["headers"] = get_token_headers()

    response = retry_request(method, **kwargs)

    if not response.ok:
        try:
            msg = click.style(str(response.json()), fg="red")
        except Exception:  # pylint: disable=broad-except
            msg = ""

        click.echo(f"Request Error: {response.url}\n{msg}")
        response.raise_for_status()

    return response


def process_api_filters(**filters):
    """
    Process filters for API.

    Arguments:
        filters (dict): name, value pairs for API filtering.

    Returns:
        dict: a `requests` formatted `params` dict.
    """
    filters = filters or {}
    filters_dict = {}

    for key, value in filters.items():
        key = key.replace(".", "__")

        if isinstance(value, (str, int, float, type(None))):
            if key == "fields" and "pk" not in value:  # pk is required
                value = ",".join(value.split(",") + ["pk"])
            filters_dict[key] = value
        elif isinstance(value, collections.Iterable):
            is_in = key.endswith("__in") or key.endswith("__in!")
            value = list(map(str, value))
            filters_dict[key] = [",".join(value)] if is_in else value
        else:  # pragma: no cover
            raise click.UsageError(f"Invalid filter: {key}, {value}")

    return filters_dict


def iterate(url, **filters):
    """
    Iterate through a paginated API endpoint and yield instances.

    Arguments:
        url (str): API URL address.
        filters (dict): name, value pairs for API filtering.

    Returns:
        list: of objects in the 'results' key of the API response.
    """
    limit = int(filters.get("limit", 5000))  # default limit to 5000 per hit
    filters["limit"] = limit
    filters = process_api_filters(**filters)
    nexturl = url
    objects = []

    while nexturl:
        filters = filters if nexturl == url else {}  # nexturl includes params
        response = api_request("get", url=nexturl, params=filters)
        results = response.json()
        nexturl = results["next"]
        objects += results["results"]

        if len(objects) >= limit:  # pragma: no cover
            message = f'retrieved {len(objects)} ouf of {results["count"]}...'
            click.echo(message, err=True)

    return objects


def get_instance(endpoint, identifier, fields=None):
    """
    Get database instance given any identifier.

    Arguments:
        identifier (str): a primary key, system_id, email or username.
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
        fields (str): comma separated list of fields to be retrieved.

    Returns:
        types.SimpleNamespace: loaded with data returned from the API.
    """
    return isablfy(
        api_request(
            "get",
            url=f"/{endpoint}/{identifier}",
            params={"fields": fields} if fields else {},
        ).json()
    )


def create_instance(endpoint, **data):
    """
    Create database instance.

    Arguments:
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
        data (dict): fields to be created.

    Returns:
        types.SimpleNamespace: loaded with data returned from the API.
    """
    return isablfy(api_request("post", url=endpoint, json=data).json())


def patch_instance(endpoint, identifier, **data):
    """
    Patch database instance.

    Arguments:
        identifier (str): a primary key, system_id, email or username.
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
        data (dict): fields to be patched.

    Returns:
        types.SimpleNamespace: loaded with data returned from the API.
    """
    run_status_change_signals = False
    run_data_import_signals = False

    if (
        endpoint == "analyses"
        and data.get("status")
        and Analysis(identifier).status != data.get("status")
    ):
        run_status_change_signals = True

    if (
        endpoint == "experiments"
        and data.get("sequencing_data")
        and Experiment(identifier).sequencing_data != data.get("sequencing_data")
    ):
        run_data_import_signals = True

    instance = api_request("patch", url=f"/{endpoint}/{identifier}", json=data).json()

    if run_status_change_signals:
        _run_signals("analyses", instance, system_settings.ON_STATUS_CHANGE)

    if run_data_import_signals:
        _run_signals("experiments", instance, system_settings.ON_DATA_IMPORT)

    return isablfy(instance)


def delete_instance(endpoint, identifier):
    """
    Delete database instance.

    Arguments:
        identifier (str): a primary key, system_id, email or username.
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
    """
    api_request("delete", url=f"/{endpoint}/{identifier}")


def get_instances(endpoint, identifiers=None, verbose=True, **filters):
    """
    Return instances from a list API endpoint.

    If not `identifiers` and not `filters` retrieves all objects in database.
    if `identifiers` and `filters`, identifieres might be filtered.

    Arguments:
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
        identifiers (list): List of identifiers.
        filters (dict): name, value pairs for API filtering.
        verbose (bool): print to stderr how many instances will be retrieved.

    Raises:
        click.UsageError: if string identifier and endpoint not in individuals,
            samples or workdflows.

    Returns:
        list: of types.SimpleNamespace objects loaded with dicts from API.
    """
    check_system_id = endpoint in {"individuals", "samples", "experiments"}
    check_name = endpoint in {"assemblies", "techniques", "tags"}
    instances = []

    if verbose:
        count = len(identifiers or [])
        count += 0 if identifiers else get_instances_count(endpoint, **filters)
        ids_msg = " at most " if identifiers else " "  # ids may be in filters
        count = f"Retrieving{ids_msg}{count} from {endpoint} API endpoint..."
        click.echo(count, err=True)

    if identifiers is None:
        instances += iterate(endpoint, **filters)
    else:
        for chunk in chunks(identifiers or [], 10000):
            filters["url"] = endpoint
            primary_keys = set()
            names = set()
            ids = set()

            for i in map(str, chunk):
                if i.isdigit():
                    primary_keys.add(i)
                elif check_name:
                    names.add(i)
                elif check_system_id:
                    ids.add(i)
                else:  # pragma: no cover
                    msg = f"msg invalid identifier for {endpoint}: {i}"
                    raise click.UsageError(msg)

            if primary_keys:
                instances += iterate(**{"pk__in": ",".join(primary_keys), **filters})

            if ids:
                instances += iterate(**{"system_id__in": ",".join(ids), **filters})

            if names:
                instances += iterate(**{"name__in": ",".join(names), **filters})

    return isablfy(instances)


def get_experiments(identifiers=None, **filters):
    """Get experiments give identifiers or filters."""
    return get_instances("experiments", identifiers=identifiers, **filters)


def get_analyses(identifiers=None, **filters):
    """Get analyses give identifiers or filters."""
    return get_instances("analyses", identifiers=identifiers, **filters)


def get_projects(identifiers=None, **filters):
    """Get projects give identifiers or filters."""
    return get_instances("projects", identifiers=identifiers, **filters)


def get_instances_count(endpoint, **filters):
    """
    Return the count of a list url.

    Arguments:
        endpoint (str): endpoint without API base URL (e.g. `analyses`).
        filters (dict): name, value pairs for API filtering.

    Returns:
        int: count of objects matching the provided filters.
    """
    filters = process_api_filters(**filters)
    filters["limit"] = 1
    return int(api_request("get", url=endpoint, params=filters).json()["count"])


def get_tree(identifier):
    """Get everything for an individual."""
    return isablfy(get_instance("individuals/tree", identifier))


def get_trees(identifiers=None, **filters):
    """Get everything for multiple individuals."""
    return isablfy(get_instances("individuals/tree", identifiers, **filters))


def patch_analyses_status(analyses, status):
    """
    Patch the `status` of multiple `analyses`.

    Arguments:
        analyses (list): of analyses instances.
        status (str): status to be updated to.

    Raises:
        AssertionError: if status not in {'SUBMITTED', 'STAGED'}.

    Returns:
        list: of updated analyses.
    """
    data = {"ids": [], "status": status, "ran_by": system_settings.api_username}
    assert status in {"SUBMITTED", "STAGED"}, f"status not supported: {status}"

    for i in analyses:  # change locally, bulk_update doesn't return instances
        i["status"] = status
        i["ran_by"] = data.get("ran_by", i["ran_by"])
        data["ids"].append(i["pk"])

    api_request("patch", url="/analyses/bulk_update/", json=data)

    for i in analyses:
        _run_signals("analyses", i, system_settings.ON_STATUS_CHANGE)

    return analyses


def patch_analysis_status(analysis, status):
    """
    Patch a successful analysis.

    Make sure analysis is owned by admin user and that results field is updated.

    Arguments:
        analysis (dict): analysis instance.
        status (dict): analysis status.

    Returns:
        dict: patched analysis instance.
    """
    data = {"status": status}
    application = analysis["application"]
    storage_url = analysis["storage_url"]

    if status in {"FAILED", "SUCCEEDED", "IN_PROGRESS"}:
        data["storage_usage"] = utils.get_tree_size(storage_url)

    # admin must own directory of regular analyses
    if status == "SUCCEEDED" and not analysis["project_level_analysis"]:
        utils.check_admin()

        if analysis["ran_by"] != system_settings.api_username:
            src = storage_url + "__tmp"
            shutil.move(storage_url, src)
            cmd = utils.get_rsync_command(src, storage_url, chmod="a-w")
            subprocess.check_call(cmd, shell=True)

    if status in {"SUCCEEDED", "IN_PROGRESS"}:
        try:
            application = import_from_string(application["application_class"])()
            data["results"] = application._get_analysis_results(analysis)
        except ImportError:
            pass
        except Exception as error:  # pragma: no cover
            data["status"] = "FAILED"
            patch_instance("analyses", analysis["pk"], **data)
            raise error

    if status == "STARTED":
        data["ran_by"] = system_settings.api_username

    return patch_instance("analyses", analysis["pk"], **data)


def _run_signals(endpoint, instance, signals):
    errors = []
    on_failure = system_settings.ON_SIGNAL_FAILURE or (lambda *_, **__: None)

    for signal in signals:
        try:
            signal(instance)
        except Exception as error:  # pragma: no cover pylint: disable=W0703
            errors.append((error, traceback.format_exc()))

            try:
                on_failure(endpoint, instance, signal, error)
            except Exception as on_failure_error:  # pylint: disable=W0703
                errors.append((on_failure_error, traceback.format_exc()))

    if errors:
        errors_dir = join(
            system_settings.BASE_STORAGE_DIRECTORY,
            ".failed_signals",
            datetime.now().year,
            datetime.now().month,
            datetime.now().day,
        )

        try:
            oldmask = os.umask(0o22)
            os.makedirs(errors_dir, exist_ok=True)
            os.umask(oldmask)
            msg = "\n".join([f"{i}:\n\t{j}" for i, j in errors])

            with open(join(errors_dir, uuid.uuid4()), "w") as f:
                f.write(msg)

        except Exception as error:  # pylint: disable=broad-except
            click.secho(f"Failed to log signal errors ({error}):\n\n{msg}", fg="red")

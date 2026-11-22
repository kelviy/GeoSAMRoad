import json
import logging
import os
import threading

import ee

log = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False

# Env vars
# when the matching env var is unset.
SERVICE_ACCOUNT_ENV = "GEE_SERVICE_ACCOUNT"        # client_email of the key
PRIVATE_KEY_ENV = "GEE_PRIVATE_KEY_FILE"           # path to the JSON key
PROJECT_ENV = "GEE_PROJECT"                        # Cloud project for the EE API

HIGH_VOLUME_ENDPOINT = "https://earthengine-highvolume.googleapis.com"


def _from_key_file(key_file):
    """``(client_email, project_id)`` out of a service-account key, or (None, None)."""
    try:
        with open(key_file) as f:
            key = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Could not read the Earth Engine key %s: %s", key_file, exc)
        return None, None
    return key.get("client_email"), key.get("project_id")


def initialize(
    project=None,
    service_account=None,
    key_file=None,
    high_volume=True,
    force=False,
):
    global _initialized
    with _lock:
        if _initialized and not force:
            return

        project = project or os.environ.get(PROJECT_ENV)
        service_account = service_account or os.environ.get(SERVICE_ACCOUNT_ENV)
        key_file = key_file or os.environ.get(PRIVATE_KEY_ENV)

        # Fill the gaps from the key itself, so pointing at a key file is enough.
        if key_file and not (service_account and project):
            email, key_project = _from_key_file(key_file)
            service_account = service_account or email
            project = project or key_project

        kwargs = {}
        if high_volume:
            kwargs["opt_url"] = HIGH_VOLUME_ENDPOINT
        if project:
            kwargs["project"] = project

        if service_account and key_file:
            log.info("Initialising Earth Engine as service account %s", service_account)
            credentials = ee.ServiceAccountCredentials(service_account, key_file)
            ee.Initialize(credentials, **kwargs)
        else:
            log.info("Initialising Earth Engine with default credentials")
            ee.Initialize(**kwargs)

        _initialized = True

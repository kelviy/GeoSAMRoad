import logging
import os
import threading

import ee

log = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False

# Env vars
SERVICE_ACCOUNT_ENV = "GEE_SERVICE_ACCOUNT"        # client_email of the key
PRIVATE_KEY_ENV = "GEE_PRIVATE_KEY_FILE"           # path to the JSON key
PROJECT_ENV = "GEE_PROJECT"                        # Cloud project for the EE API

HIGH_VOLUME_ENDPOINT = "https://earthengine-highvolume.googleapis.com"


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

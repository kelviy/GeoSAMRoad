"""The plugin's client for the review service (``review_service`` in the
repo), which runs the review core in a Python that has its dependencies.

No QGIS import, so the repo's tests drive it against a real service.
"""

import json
import os
import subprocess
import tempfile

SERVICE_MODULE = "sentinel2data.cleaning.review_service"

# QGIS sets these for its own Python. Passed on, they would make the service's
# Python load QGIS's standard library and packages instead of its own.
QGIS_PYTHON_VARIABLES = ("PYTHONHOME", "PYTHONPATH")


class ServiceError(Exception):
    """The review service could not start, stopped, or gave no answer."""


class ReviewClient:
    """One review service process, started on the first request and again
    after it stops. Each request waits for its answer."""

    def __init__(self, python: str):
        self.python = python
        self._process: subprocess.Popen[str] | None = None
        self._errors = None

    def ask(self, op: str, gpkg, **args) -> dict:
        """The service's answer to ``op`` on ``gpkg``: a dict with ``ok`` and,
        on success, ``state``, or ``error`` and ``error_type``."""
        process = self._started()
        assert process.stdin and process.stdout  # both are pipes
        request = {"op": op, "gpkg": str(gpkg), **args}
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except OSError:
            line = ""
        if not line:
            raise ServiceError(f"The review service stopped:\n{self._stop()}")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise ServiceError(
                f"The review service answered with something other than JSON: {line!r}"
            ) from exc

    def close(self):
        self._stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _started(self):
        if self._process is None or self._process.poll() is not None:
            self._stop()
            env = {
                k: v for k, v in os.environ.items() if k not in QGIS_PYTHON_VARIABLES
            }
            self._errors = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 (lives as long as the process; _stop closes it)
            try:
                self._process = subprocess.Popen(
                    [self.python, "-m", SERVICE_MODULE],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self._errors,
                    text=True,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise ServiceError(
                    f"Can't start the review service with {self.python}: {exc}"
                ) from exc
        return self._process

    def _stop(self) -> str:
        """Stop the process, if any, and return the end of its error output."""
        process, self._process = self._process, None
        if process is not None:
            assert process.stdin and process.stdout
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        errors, self._errors = self._errors, None
        if errors is None:
            return ""
        errors.seek(0)
        tail = errors.read()[-4000:]
        errors.close()
        return tail


def service_python(plugin_dir) -> str:
    """The Python that runs the review service: ``ROSA_REVIEW_PYTHON`` when
    set, else the one ``install-qgis-plugin`` wrote into ``service.json``."""
    if os.environ.get("ROSA_REVIEW_PYTHON"):
        return os.environ["ROSA_REVIEW_PYTHON"]
    path = os.path.join(plugin_dir, "service.json")
    try:
        with open(path) as f:
            return json.load(f)["python"]
    except (OSError, ValueError, KeyError) as exc:
        raise ServiceError(
            f"No review service Python in {path} ({exc}). Run "
            "`python -m sentinel2data.cli install-qgis-plugin` from the repo, or set "
            "ROSA_REVIEW_PYTHON."
        ) from exc

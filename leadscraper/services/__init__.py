"""The services layer: the one core under the HTTP API, the CLI and the worker.

APP-BUILD-PROMPT A.3: "the FastAPI app, the CLI and the worker are thin
layers over the same service/core modules". Every operation a user can ask
for -- create a project, save a config, queue a run, diff two runs, export,
import a suppression list, bundle a workspace -- is a function here taking an
Engine and a :class:`~leadscraper.services.workspace.Workspace`. Routers and
CLI commands only parse input, call one of these, and render the result.

Errors are typed (``errors.py``) so each layer maps them its own way: the API
to 404 / 409 / 422, the CLI to an exit code and a message.
"""

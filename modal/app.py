import asyncio
import io
import os
import sys
from typing import Collection, Dict, Optional

from rich.tree import Tree

from modal_proto import api_pb2
from modal_utils.async_utils import TaskContext, synchronize_apis, synchronizer
from modal_utils.decorator_utils import decorator_with_options

from ._app_singleton import get_container_app, set_container_app
from ._app_state import AppState
from ._blueprint import Blueprint
from ._function_utils import FunctionInfo
from ._output import OutputManager, step_completed, step_progress
from ._serialization import Pickler, Unpickler
from .client import _Client
from .config import config, logger
from .exception import InvalidError, NotFoundError
from .functions import _Function, _FunctionProxy
from .image import _DebianSlim, _Image
from .mount import MODAL_CLIENT_MOUNT_NAME, _create_client_mount, _Mount
from .object import Object, ref
from .rate_limit import RateLimit
from .schedule import Schedule
from .secret import Secret


class _App:
    """An App manages Objects (Functions, Images, Secrets, Schedules etc.) associated with your applications

    The App has three main responsibilities:
    * Syncing of identities across processes (your local Python interpreter and every Modal worker active in your application)
    * Making Objects stay alive and not be garbage collected for as long as the app lives (see App lifetime below)
    * Manage log collection for everything that happens inside your code

    **Registering Functions with an app**

    The most common way to explicitly register an Object with an app is through the `app.function()` decorator.
    It both registers the annotated function itself and other passed objects like Schedules and Secrets with the
    specified app:

    ```python
    import modal

    app = modal.App()

    @app.function(secret=modal.ref("some_secret"), schedule=modal.Period(days=1))
    def foo():
        ...
    ```
    In this example, both `foo`, the secret and the schedule are registered with the app.
    """

    _tag_to_object: Dict[str, Object]
    _tag_to_existing_id: Dict[str, str]

    @classmethod
    def _initialize_container_app(cls):
        set_container_app(super().__new__(cls))

    def __new__(cls, *args, **kwargs):
        singleton = get_container_app()
        if singleton is not None and cls == _App:
            # If there's a singleton app, just return it for everything
            assert isinstance(singleton, cls)
            return singleton
        else:
            # Refer to the normal constructor
            app = super().__new__(cls)
            return app

    def __init__(self, name=None):
        if "_initialized" in self.__dict__:
            return  # Prevent re-initialization with the singleton

        self._initialized = True
        self._app_id = None
        self.client = None
        # TODO: we take a name in the app constructor, that can be different from the deployment name passed in later. Simplify this.
        self._name = name
        self.deployment_name = None
        self.state = AppState.NONE
        self._tag_to_object = {}
        self._tag_to_existing_id = {}
        self._blueprint = Blueprint()
        # TODO: this is only used during _flush_objects, but that function gets called from objects, so we need to store it somewhere
        # Once we rewrite object creation to be non-recursive, this should no longer be needed
        self._progress: Optional[Tree] = None
        super().__init__()

    # needs to be a function since synchronicity hides other attributes.
    def provided_name(self):
        return self._name

    @property
    def name(self):
        return self._name or self._infer_app_name()

    @property
    def app_id(self):
        return self._app_id

    def _infer_app_name(self):
        script_filename = os.path.split(sys.argv[0])[-1]
        args = [script_filename] + sys.argv[1:]
        return " ".join(args)

    async def _initialize_container(self, app_id, client, task_id):
        """Used by the container to bootstrap the app and all its objects."""
        self._app_id = app_id
        self.client = client

        req = api_pb2.AppGetObjectsRequest(app_id=app_id, task_id=task_id)
        resp = await self.client.stub.AppGetObjects(req)
        for (
            tag,
            object_id,
        ) in resp.object_ids.items():
            self._tag_to_object[tag] = Object.from_id(object_id, self)

        # In the container, run forever
        self.state = AppState.RUNNING

    async def lookup(self, obj: Object) -> str:
        """Takes a Ref object and looks up its id.

        It's either an object defined locally on this app, or one defined on a separate app
        """
        if not obj.label:
            # TODO: explain these exception more, since I think it might be a common issue
            raise InvalidError(f"Object {obj} has no label. Make sure every object is defined on the app.")
        if not obj.label.app_name and not obj.label.object_label:
            raise InvalidError(f"Object label {obj.label} is a malformed reference to nothing.")

        if obj.label.app_name is not None:
            # A different app
            object_id = await self._include(obj.label.app_name, obj.label.object_label, obj.label.namespace)
        else:
            # Same app, an object that was created earlier
            obj = self._tag_to_object[obj.label.object_label]
            object_id = obj.object_id

        assert object_id
        return object_id

    async def _create_object(self, obj: Object, existing_object_id: Optional[str] = None) -> str:
        """Takes an object as input, create it, and return an object id."""
        creating_message = obj.get_creating_message()
        if creating_message is not None:
            step_node = self._progress.add(step_progress(creating_message))

        if obj.label is not None:
            assert obj.label.app_name is not None
            # A different app
            object_id = await self._include(obj.label.app_name, obj.label.object_label, obj.label.namespace)

        else:
            # Create object
            object_id = await obj.load(self, existing_object_id)
            if existing_object_id is not None and object_id != existing_object_id:
                # TODO(erikbern): this is a very ugly fix to a problem that's on the server side.
                # Unlike every other object, images are not assigned random ids, but rather an
                # id given by the hash of its contents. This means we can't _force_ an image to
                # have a particular id. The better solution is probably to separate "images"
                # from "image definitions" or something like that, but that's a big project.
                if not existing_object_id.startswith("im-"):
                    raise Exception(
                        f"Tried creating an object using existing id {existing_object_id} but it has id {object_id}"
                    )
        if object_id is None:
            raise Exception(f"object_id for object of type {type(obj)} is None")

        if creating_message is not None:
            created_message = obj.get_created_message()
            assert created_message is not None
            step_node.label = step_completed(created_message, is_substep=True)

        return object_id

    async def _create_all_objects(self):
        """Create objects that have been defined but not created on the server."""
        # Instead of doing a topological sort here, we rely on a sort of dumb "trick".
        # Functions are the only objects that "depend" on other objects, so we make sure
        # they are built last. In the future we might have some more complicated structure
        # where we actually have to model out the DAG
        tags = [tag for tag, obj in self._blueprint.get_objects()]
        tags.sort(key=lambda obj: obj.startswith("fu-"))
        for tag in tags:
            obj = self._blueprint.get_object(tag)
            existing_object_id = self._tag_to_existing_id.get(tag)
            logger.debug(f"Creating object {tag} with existing id {existing_object_id}")
            object_id = await self._create_object(obj, existing_object_id)
            self._tag_to_object[tag] = Object.from_id(object_id, self)

    def __getitem__(self, tag: str):
        assert isinstance(tag, str)
        # TODO(erikbern): this should really be an app vs blueprint thing
        if self.state == AppState.RUNNING:
            # TODO: this is a terrible hack for now. For running apps inside the container,
            # because of the singleton thing, any unrelated app will also be RUNNING, so this
            # branch triggers. However we don't want this to cause a KeyError.
            # Let's revisit once we clean up the app singleton
            return self._tag_to_object.get(tag)
        else:
            # Return a reference to an object that will be created in the future
            return ref(None, tag)

    def __setitem__(self, tag, obj):
        if obj.label and not obj.label.app_name:
            raise Exception("Setting a reference on the blueprint")
        self._blueprint.register(tag, obj)

    @synchronizer.asynccontextmanager
    async def _run(self, client, output_mgr, existing_app_id, last_log_entry_id=None):
        # TOOD: use something smarter than checking for the .client to exists in order to prevent
        # race conditions here!
        if self.state != AppState.NONE:
            raise Exception(f"Can't start a app that's already in state {self.state}")
        self.state = AppState.STARTING
        self.client = client

        try:
            if existing_app_id is not None:
                # Get all the objects first
                obj_req = api_pb2.AppGetObjectsRequest(app_id=existing_app_id)
                obj_resp = await self.client.stub.AppGetObjects(obj_req)
                self._tag_to_existing_id = dict(obj_resp.object_ids)
                self._app_id = existing_app_id
            else:
                # Start app
                # TODO(erikbern): maybe this should happen outside of this method?
                app_req = api_pb2.AppCreateRequest(client_id=client.client_id, name=self.name)
                app_resp = await client.stub.AppCreate(app_req)
                self._tag_to_existing_id = {}
                self._app_id = app_resp.app_id

            # Start tracking logs and yield context
            async with TaskContext(grace=config["logs_timeout"]) as tc:
                with output_mgr.ctx_if_visible(output_mgr.make_live(step_progress("Initializing..."))):
                    live_task_status = output_mgr.make_live(step_progress("Running app..."))
                    tc.create_task(
                        output_mgr.get_logs_loop(self._app_id, self.client, live_task_status, last_log_entry_id or "")
                    )
                output_mgr.print_if_visible(step_completed("Initialized."))

                try:
                    progress = Tree(step_progress("Creating objects..."), guide_style="gray50")
                    self._progress = progress
                    with output_mgr.ctx_if_visible(output_mgr.make_live(progress)):
                        await self._create_all_objects()
                    progress.label = step_completed("Created objects.")
                    output_mgr.print_if_visible(progress)

                    # Create all members
                    with output_mgr.ctx_if_visible(live_task_status):
                        # Create the app (and send a list of all tagged obs)
                        # TODO(erikbern): we should delete objects from a previous version that are no longer needed
                        # We just delete them from the app, but the actual objects will stay around
                        object_ids = {tag: obj.object_id for tag, obj in self._tag_to_object.items()}
                        req_set = api_pb2.AppSetObjectsRequest(
                            app_id=self._app_id,
                            object_ids=object_ids,
                        )
                        await self.client.stub.AppSetObjects(req_set)

                        self.state = AppState.RUNNING
                        yield self  # yield context manager to block
                        self.state = AppState.STOPPING

                finally:
                    # Stop app server-side. This causes:
                    # 1. Server to kill any running task
                    # 2. Logs to drain (stopping the _get_logs_loop coroutine)
                    logger.debug("Stopping the app server-side")
                    req_disconnect = api_pb2.AppClientDisconnectRequest(app_id=self._app_id)
                    await self.client.stub.AppClientDisconnect(req_disconnect)

            output_mgr.print_if_visible(step_completed("App completed."))

        finally:
            self.client = None
            self.state = AppState.NONE
            self._progress = None
            self._tag_to_object = {}

    @synchronizer.asynccontextmanager
    async def _get_client(self, client=None):
        if client is None:
            async with _Client.from_env() as client:
                yield client
        else:
            yield client

    @synchronizer.asynccontextmanager
    async def run(self, client=None, stdout=None, show_progress=None):
        async with self._get_client(client) as client:
            output_mgr = OutputManager(stdout, show_progress)
            async with self._run(client, output_mgr, None) as it:
                yield it  # ctx mgr

    async def run_forever(self, client=None, stdout=None, show_progress=None):
        async with self._get_client(client) as client:
            output_mgr = OutputManager(stdout, show_progress)
            async with self._run(client, output_mgr, None):
                timeout = config["run_forever_timeout"]
                if timeout:
                    output_mgr.print_if_visible(step_completed(f"Running for {timeout} seconds... hit Ctrl-C to stop!"))
                    await asyncio.sleep(timeout)
                else:
                    output_mgr.print_if_visible(step_completed("Running forever... hit Ctrl-C to stop!"))
                    while True:
                        await asyncio.sleep(1.0)

    async def detach(self):
        request = api_pb2.AppDetachRequest(app_id=self._app_id)
        await self.client.stub.AppDetach(request)

    async def deploy(
        self,
        name: str = None,  # Unique name of the deployment. Subsequent deploys with the same name overwrites previous ones. Falls back to the app name
        namespace=api_pb2.DEPLOYMENT_NAMESPACE_ACCOUNT,
        client=None,
        stdout=None,
        show_progress=None,
    ):
        """Deploys and exports objects in the app

        Usage:
        ```python
        if __name__ == "__main__":
            app.deploy()
        ```

        Deployment has two primary purposes:
        * Persists all of the objects (Functions, Images, Schedules etc.) in the app, allowing them to live past the current app run
          Notably for Schedules this enables headless "cron"-like functionality where scheduled functions continue to be invoked after
          the client has closed.
        * Allows for certain of these objects, *deployment objects*, to be referred to and used by other apps
        """
        if self.state != AppState.NONE:
            raise InvalidError("Can only deploy an app that isn't running")
        if name is None:
            name = self.name
        if name is None:
            raise InvalidError(
                "You need to either supply an explicit deployment name to the deploy command, or have a name set on the app.\n"
                "\n"
                "Examples:\n"
                'app.deploy("some_name")\n\n'
                "or\n"
                'app = App("some-name")'
            )

        self.deployment_name = name

        async with self._get_client(client) as client:
            # Look up any existing deployment
            app_req = api_pb2.AppGetByDeploymentNameRequest(name=name, namespace=namespace, client_id=client.client_id)
            app_resp = await client.stub.AppGetByDeploymentName(app_req)
            existing_app_id = app_resp.app_id or None
            last_log_entry_id = app_resp.last_log_entry_id

            # The `_run` method contains the logic for starting and running an app
            output_mgr = OutputManager(stdout, show_progress)
            async with self._run(client, output_mgr, existing_app_id, last_log_entry_id):
                # TODO: this could be simplified in case it's the same app id as previously
                deploy_req = api_pb2.AppDeployRequest(
                    app_id=self._app_id,
                    name=name,
                    namespace=namespace,
                )
                await client.stub.AppDeploy(deploy_req)
        return self._app_id

    async def _include(self, name, object_label, namespace):
        """Internal method to resolve to an object id."""
        request = api_pb2.AppIncludeObjectRequest(
            app_id=self._app_id,
            name=name,
            object_label=object_label,
            namespace=namespace,
        )
        response = await self.client.stub.AppIncludeObject(request)
        if not response.object_id:
            obj_repr = name
            if object_label is not None:
                obj_repr += f".{object_label}"
            if namespace != api_pb2.DEPLOYMENT_NAMESPACE_ACCOUNT:
                obj_repr += f" (namespace {api_pb2.DeploymentNamespace.Name(namespace)})"
            # TODO: disambiguate between app not found and object not found?
            err_msg = f"Could not find object {obj_repr}"
            raise NotFoundError(err_msg, obj_repr)
        return response.object_id

    async def include(self, name, object_label=None, namespace=api_pb2.DEPLOYMENT_NAMESPACE_ACCOUNT):
        """Looks up an object and return a newly constructed one."""
        object_id = await self._include(name, object_label, namespace)
        return Object.from_id(object_id, self)

    def _serialize(self, obj):
        """Serializes object and replaces all references to the client class by a placeholder."""
        buf = io.BytesIO()
        Pickler(self, buf).dump(obj)
        return buf.getvalue()

    def _deserialize(self, s: bytes):
        """Deserializes object and replaces all client placeholders by self."""
        return Unpickler(self, io.BytesIO(s)).load()

    def _register_function(self, function):
        self._blueprint.register(function.tag, function)
        function_proxy = _FunctionProxy(function, self, function.tag)
        return function_proxy

    def _get_default_image(self):
        # TODO(erikbern): instead of writing this to the same namespace
        # as the user's objects, we could use sub-blueprints in the future
        if not self._blueprint.has_object("_image"):
            self._blueprint.register("_image", _DebianSlim())
        return self._blueprint.get_object("_image")

    def _get_function_mounts(self, raw_f):
        mounts = []

        # Create client mount
        if not self._blueprint.has_object("_client_mount"):
            if config["sync_entrypoint"]:
                client_mount = _create_client_mount()
            else:
                client_mount = ref(MODAL_CLIENT_MOUNT_NAME, namespace=api_pb2.DEPLOYMENT_NAMESPACE_GLOBAL)
            self._blueprint.register("_client_mount", client_mount)
        mounts.append(ref(None, "_client_mount"))

        # Create function mounts
        info = FunctionInfo(raw_f)
        for key, mount in info.get_mounts().items():
            if not self._blueprint.has_object(key):
                self._blueprint.register(key, mount)
            mounts.append(ref(None, key))

        return mounts

    @decorator_with_options
    def function(
        self,
        raw_f=None,  # The decorated function
        *,
        image: _Image = None,  # The image to run as the container for the function
        schedule: Optional[Schedule] = None,  # An optional Modal Schedule for the function
        secret: Optional[Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: bool = False,  # Whether a GPU is required
        rate_limit: Optional[RateLimit] = None,  # Optional RateLimit for the function
        serialized: bool = False,  # Whether to send the function over using cloudpickle.
        mounts: Collection[_Mount] = (),
    ) -> _Function:  # Function object - callable as a regular function within a Modal app
        """Decorator to create Modal functions"""
        if image is None:
            image = self._get_default_image()
        mounts = [*self._get_function_mounts(raw_f), *mounts]
        function = _Function(
            raw_f,
            image=image,
            secret=secret,
            secrets=secrets,
            schedule=schedule,
            is_generator=False,
            gpu=gpu,
            rate_limit=rate_limit,
            serialized=serialized,
            mounts=mounts,
        )
        return self._register_function(function)

    @decorator_with_options
    def generator(
        self,
        raw_f=None,  # The decorated function
        *,
        image: _Image = None,  # The image to run as the container for the function
        secret: Optional[Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: bool = False,  # Whether a GPU is required
        rate_limit: Optional[RateLimit] = None,  # Optional RateLimit for the function
        serialized: bool = False,  # Whether to send the function over using cloudpickle.
        mounts: Collection[_Mount] = (),
    ) -> _Function:
        """Decorator to create Modal generators"""
        if image is None:
            image = self._get_default_image()
        mounts = [*self._get_function_mounts(raw_f), *mounts]
        function = _Function(
            raw_f,
            image=image,
            secret=secret,
            secrets=secrets,
            is_generator=True,
            gpu=gpu,
            rate_limit=rate_limit,
            serialized=serialized,
            mounts=mounts,
        )
        return self._register_function(function)

    @decorator_with_options
    def asgi(
        self,
        asgi_app,  # The asgi app
        *,
        wait_for_response: bool = True,  # Whether requests should wait for and return the function response.
        image: _Image = None,  # The image to run as the container for the function
        secret: Optional[Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: bool = False,  # Whether a GPU is required
        mounts: Collection[_Mount] = (),
    ):
        if image is None:
            image = self._get_default_image()
        mounts = [*self._get_function_mounts(asgi_app), *mounts]
        function = _Function(
            asgi_app,
            image=image,
            secret=secret,
            secrets=secrets,
            is_generator=False,
            gpu=gpu,
            mounts=mounts,
            webhook_config=api_pb2.WebhookConfig(
                type=api_pb2.WEBHOOK_TYPE_ASGI_APP, wait_for_response=wait_for_response
            ),
        )
        return self._register_function(function)

    @decorator_with_options
    def webhook(
        self,
        raw_f,
        *,
        method: str = "GET",  # REST method for the created endpoint.
        wait_for_response: bool = True,  # Whether requests should wait for and return the function response.
        image: _Image = None,  # The image to run as the container for the function
        secret: Optional[Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: bool = False,  # Whether a GPU is required
        mounts: Collection[_Mount] = (),
    ):
        if image is None:
            image = self._get_default_image()
        mounts = [*self._get_function_mounts(raw_f), *mounts]
        function = _Function(
            raw_f,
            image=image,
            secret=secret,
            secrets=secrets,
            is_generator=False,
            gpu=gpu,
            mounts=mounts,
            webhook_config=api_pb2.WebhookConfig(
                type=api_pb2.WEBHOOK_TYPE_FUNCTION, method=method, wait_for_response=wait_for_response
            ),
        )
        return self._register_function(function)

    async def interactive_shell(self, image_ref, cmd=None, mounts=[], secrets=[]):
        """Run `cmd` interactively within this image. Similar to `docker run -it --entrypoint={cmd}`.

        If `cmd` is `None`, this falls back to the default shell within the image.
        """
        from ._image_pty import image_pty

        await image_pty(image_ref, self, cmd, mounts, secrets)


App, AioApp = synchronize_apis(_App)


def is_local() -> bool:
    """Returns whether we're running in the cloud or not."""
    return not get_container_app()

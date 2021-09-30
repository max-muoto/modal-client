import asyncio
import cloudpickle
import importlib
import inspect
import os
import sys
import uuid

from .async_utils import retry, synchronizer
from .client import Client
from .config import config, logger
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIMEOUT
from .mount import Mount, create_package_mounts
from .object import Object, requires_join, requires_join_generator
from .proto import api_pb2
from .queue import Queue
from .session import Session


def _function_to_path(f):
    function_name = f.__name__
    module = inspect.getmodule(f)
    if module.__package__:
        # This is a "real" module, eg. examples.logs.f
        # Get the package path
        package_path = __import__(module.__package__).__path__
        # TODO: we should handle the array case, https://stackoverflow.com/questions/2699287/what-is-path-useful-for
        assert len(package_path) == 1
        (package_path,) = package_path
        module_name = module.__spec__.name
        recursive_upload = True
        remote_dir = "/root/" + module.__package__  # TODO: don't hardcode /root
    else:
        # This generally covers the case where it's invoked with
        # python foo/bar/baz.py
        module_name = os.path.splitext(os.path.basename(module.__file__))[0]
        package_path = os.path.dirname(module.__file__)
        recursive_upload = False  # Just pick out files in the same directory
        remote_dir = "/root"  # TODO: don't hardcore /root

    # Create mount
    mount = Mount(
        local_dir=package_path,
        remote_dir=remote_dir,
        condition=lambda filename: os.path.splitext(filename)[1] == ".py",
        recursive=recursive_upload,
    )

    return (mount, module_name, function_name)


def _path_to_function(module_name, function_name):
    try:
        module = importlib.import_module(module_name)
        return getattr(module, function_name)
    except ModuleNotFoundError:
        # Just print some debug stuff, then re-raise
        logger.info(f"{os.getcwd()=}")
        logger.info(f"{sys.path=}")
        logger.info(f"{os.listdir()=}")
        raise


class Call(Object):
    # TODO: I'm increasingly skeptical that this should in fact be its own object, but let's revisit
    def __init__(self, function_id, inputs, window, kwargs):
        super().__init__(
            args=dict(
                function_id=function_id,
                inputs=inputs,
                window=window,
                kwargs=kwargs,
            ),
        )

    async def _join(self):
        # TODO: This is incredibly dumb. The server API lets us enqueue and create a call_id lazily
        # To get around the current structure where joining is separate from other methods,
        # we create a call by enqueueing zero inputs
        request = api_pb2.FunctionCallRequest(
            function_id=self.args.function_id,
            inputs=[],
            idempotency_key=str(uuid.uuid4()),
            call_id=None,
        )
        response = await retry(self.client.stub.FunctionCall)(request)
        return response.call_id

    async def _enqueue(self, args, kwargs):
        # TODO: break out the creation of the call into a separate request
        request = api_pb2.FunctionCallRequest(
            function_id=self.args.function_id,
            inputs=[self.client.serialize((arg, kwargs)) for arg in args],
            idempotency_key=str(uuid.uuid4()),
            call_id=self.object_id,
        )
        response = await retry(self.client.stub.FunctionCall)(request)

    async def _dequeue(self, n_outputs):
        while True:
            request = api_pb2.FunctionGetNextOutputRequest(
                function_id=self.args.function_id,  # TODO: why is this needed?
                call_id=self.object_id,
                timeout=BLOCKING_REQUEST_TIMEOUT,
                idempotency_key=str(uuid.uuid4()),
                n_outputs=n_outputs,
            )
            response = await retry(self.client.stub.FunctionGetNextOutput)(request, timeout=GRPC_REQUEST_TIMEOUT)
            if response.outputs:
                break
        for output in response.outputs:
            if output.status != api_pb2.GenericResult.Status.SUCCESS:
                raise Exception("Remote exception: %s\n%s" % (output.exception, output.traceback))
            yield self.client.deserialize(output.data)

    async def __aiter__(self):
        # Most of the complexity of this function comes from the input throttling.
        # Basically the idea is that we maintain x (default 100) outstanding requests at any point in time,
        # and we don't enqueue more requests until we get back enough values.
        # It probably makes a lot of sense to move the input throttling to the server instead.

        # TODO: we should support asynchronous generators as well
        inputs = iter(self.args.inputs)  # Handle non-generator inputs

        n_enqueued, n_dequeued = 0, 0
        input_exhausted = False
        while not input_exhausted or n_dequeued < n_enqueued:
            logger.debug("Map status: %d enqueued, %d dequeued" % (n_enqueued, n_dequeued))
            batch_args = []
            while not input_exhausted and n_enqueued < n_dequeued + self.args.window:
                try:
                    batch_args.append(next(inputs))
                    n_enqueued += 1
                except StopIteration:
                    input_exhausted = True
            if batch_args:
                await self._enqueue(batch_args, self.args.kwargs)
            if n_dequeued < n_enqueued:
                async for output in self._dequeue(n_enqueued - n_dequeued):
                    n_dequeued += 1
                    yield output


class Function(Object):
    def __init__(self, raw_f, image=None, client=None):
        assert callable(raw_f)
        super().__init__(
            args=dict(
                raw_f=raw_f,
                image=image,
            ),
        )

    async def _join(self):
        mount, module_name, function_name = _function_to_path(self.args.raw_f)

        mounts = [mount]
        if config["sync_entrypoint"] and not os.getenv("POLYESTER_IMAGE_LOCAL_ID"):
            # TODO(erikbern): If the first condition is true then we're running in a local
            # client which implies the second is always true as well?
            mounts.extend(create_package_mounts("polyester"))
        # TODO(erikbern): couldn't we just create one single mount with all packages instead of multiple?

        # Wait for image and mounts to finish
        image = await self.args.image.join(self.client, self.session)
        mounts = await asyncio.gather(*(mount.join(self.client, self.session) for mount in mounts))

        # Create function remotely
        function_definition = api_pb2.Function(
            module_name=module_name,
            function_name=function_name,
            mount_ids=[mount.object_id for mount in mounts],
        )
        request = api_pb2.FunctionGetOrCreateRequest(
            session_id=self.session.session_id,
            image_id=image.object_id,  # TODO: move into the function definition?
            function=function_definition,
        )
        response = await self.client.stub.FunctionGetOrCreate(request)
        return response.function_id

    @requires_join_generator
    async def map(self, inputs, window=100, kwargs={}):
        args = [(arg,) for arg in inputs]
        call = Call(self.object_id, args, window, kwargs)
        call_joined = await call.join(self.client, self.session)
        return call_joined

    @requires_join
    async def __call__(self, *args, **kwargs):
        call = Call(self.object_id, [args], window=1, kwargs=kwargs)
        call_joined = await call.join(self.client, self.session)
        async for output in call_joined:
            return output  # return the first (and only) one

    @staticmethod
    def get_function(module_name, function_name):
        f = _path_to_function(module_name, function_name)
        assert isinstance(f, Function)
        return f.args.raw_f


def decorate_function(raw_f, image):
    if callable(raw_f):
        return Function(raw_f=raw_f, image=image)
    else:
        raise Exception("%s is not a proper function (of type %s)" % (raw_f, type(raw_f)))

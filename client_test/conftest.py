import asyncio
import contextlib
import os
import pytest
import shutil
import tempfile
import typing

import cloudpickle
import grpc
import pkg_resources
from google.protobuf.empty_pb2 import Empty
from grpc import StatusCode
from grpc.aio import ServicerContext

from modal._app_singleton import set_container_app
from modal.client import AioClient, Client
from modal.functions import MODAL_CLIENT_MOUNT_NAME
from modal.image import _dockerhub_python_version
from modal.version import __version__
from modal_proto import api_pb2, api_pb2_grpc
from modal_utils.async_utils import synchronize_apis


class GRPCClientServicer(api_pb2_grpc.ModalClient):
    def __init__(self):
        self.requests = []
        self.done = False
        self.container_inputs = []
        self.container_outputs = []
        self.object_ids = {}
        self.queue = []
        self.deployments = {
            MODAL_CLIENT_MOUNT_NAME: "mo-123",
            "foo-queue": "qu-foo",
            f"debian-slim-{_dockerhub_python_version()}": "im-123",
        }
        self.n_queues = 0
        self.files_name2sha = {}
        self.files_sha2data = {}
        self.client_calls = []
        self.n_functions = 0
        self.n_schedules = 0
        self.function2schedule = {}
        self.function_create_error = False
        self.heartbeat_return_client_gone = False

    async def ClientCreate(
        self, request: api_pb2.ClientCreateRequest, context: ServicerContext = None, timeout=None
    ) -> api_pb2.ClientCreateResponse:
        self.requests.append(request)
        client_id = "cl-123"
        if pkg_resources.parse_version(request.version) < pkg_resources.parse_version(__version__):
            await context.abort(StatusCode.FAILED_PRECONDITION, "Old client")
        return api_pb2.ClientCreateResponse(client_id=client_id)

    async def AppCreate(
        self,
        request: api_pb2.AppCreateRequest,
        context: ServicerContext = None,
    ) -> api_pb2.AppCreateResponse:
        self.requests.append(request)
        app_id = "se-123"
        return api_pb2.AppCreateResponse(app_id=app_id)

    async def AppClientDisconnect(
        self, request: api_pb2.AppClientDisconnectRequest, context: ServicerContext = None
    ) -> Empty:
        self.requests.append(request)
        self.done = True
        return Empty()

    async def ClientHeartbeat(self, request: api_pb2.ClientHeartbeatRequest, context: ServicerContext = None) -> Empty:
        self.requests.append(request)
        if self.heartbeat_return_client_gone:
            await context.abort(StatusCode.NOT_FOUND, f"Client {request.client_id} not found")
        return api_pb2.ClientHeartbeatResponse(seconds_since_last=1.0)

    async def ImageGetOrCreate(
        self, request: api_pb2.ImageGetOrCreateRequest, context: ServicerContext
    ) -> api_pb2.ImageGetOrCreateResponse:
        self.last_image = request.image
        return api_pb2.ImageGetOrCreateResponse(image_id="im-123")

    async def ImageJoin(self, request: api_pb2.ImageJoinRequest, context: ServicerContext) -> api_pb2.ImageJoinResponse:
        return api_pb2.ImageJoinResponse(
            result=api_pb2.GenericResult(status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS)
        )

    async def AppGetLogs(
        self, request: api_pb2.AppGetLogsRequest, context: ServicerContext = None, timeout=None
    ) -> typing.AsyncIterator[api_pb2.TaskLogsBatch]:
        await asyncio.sleep(0.1)
        if self.done:
            yield api_pb2.TaskLogsBatch(app_state=api_pb2.APP_STATE_STOPPED)

    async def FunctionGetInputs(
        self, request: api_pb2.FunctionGetInputsRequest, context: ServicerContext = None
    ) -> api_pb2.FunctionGetInputsResponse:
        return self.container_inputs.pop(0)

    async def FunctionPutOutputs(
        self, request: api_pb2.FunctionPutOutputsRequest, context: ServicerContext = None
    ) -> api_pb2.FunctionPutOutputsResponse:
        self.container_outputs.append(request)
        return api_pb2.FunctionPutOutputsResponse(status=api_pb2.WRITE_STATUS_SUCCESS)

    async def AppGetObjects(
        self, request: api_pb2.AppGetObjectsRequest, context: ServicerContext = None
    ) -> api_pb2.AppGetObjectsResponse:
        return api_pb2.AppGetObjectsResponse(object_ids=self.object_ids)

    async def AppSetObjects(self, request: api_pb2.AppSetObjectsRequest, context: ServicerContext = None) -> Empty:
        self.objects = dict(request.object_ids)
        return Empty()

    async def QueueCreate(
        self, request: api_pb2.QueueCreateRequest, context: ServicerContext = None
    ) -> api_pb2.QueueCreateResponse:
        self.n_queues += 1
        return api_pb2.QueueCreateResponse(queue_id=f"qu-{self.n_queues}")

    async def QueuePut(self, request: api_pb2.QueuePutRequest, context: ServicerContext = None) -> Empty:
        self.queue += request.values
        return Empty()

    async def QueueGet(
        self, request: api_pb2.QueueGetRequest, context: ServicerContext = None
    ) -> api_pb2.QueueGetResponse:
        return api_pb2.QueueGetResponse(values=[self.queue.pop(0)])

    async def AppDeploy(self, request: api_pb2.AppDeployRequest, context: ServicerContext = None) -> Empty:
        if request.object_id:
            self.deployments[request.name] = request.object_id
        elif request.object_ids:
            for label, object_id in request.object_ids.items():
                self.deployments[(request.name, label)] = object_id
        else:
            self.deployments[request.name] = ""  # for stuff like schedules that don't require arguments to deploy
        return Empty()

    async def AppIncludeObject(
        self, request: api_pb2.AppIncludeObjectRequest, context: ServicerContext
    ) -> api_pb2.AppIncludeObjectResponse:
        if request.object_label:
            object_id = self.deployments.get((request.name, request.object_label))
        else:
            object_id = self.deployments.get(request.name)
        return api_pb2.AppIncludeObjectResponse(object_id=object_id)

    async def MountCreate(
        self,
        request: api_pb2.MountCreateRequest,
        context: ServicerContext,
    ) -> api_pb2.MountCreateResponse:
        return api_pb2.MountCreateResponse(mount_id="mo-123")

    async def MountRegisterFile(
        self,
        request: api_pb2.MountRegisterFileRequest,
        context: ServicerContext,
    ) -> api_pb2.MountRegisterFileResponse:
        self.files_name2sha[request.filename] = request.sha256_hex
        return api_pb2.MountRegisterFileResponse(filename=request.filename, exists=False)

    async def MountUploadFile(
        self,
        request: api_pb2.MountUploadFileRequest,
        context: ServicerContext,
    ) -> Empty:
        self.files_sha2data[request.sha256_hex] = request.data
        return Empty()

    async def MountDone(
        self,
        request: api_pb2.MountDoneRequest,
        context: ServicerContext,
    ) -> Empty:
        return Empty()

    async def FunctionCreate(
        self,
        request: api_pb2.FunctionCreateRequest,
        context: ServicerContext,
    ) -> api_pb2.FunctionCreateResponse:
        if self.function_create_error:
            raise Exception("Function create failed")
        self.n_functions += 1
        function_id = f"fu-{self.n_functions}"
        if request.schedule:
            self.function2schedule[function_id] = request.schedule
        return api_pb2.FunctionCreateResponse(function_id=function_id)

    async def FunctionMap(
        self,
        request: api_pb2.FunctionMapRequest,
        context: ServicerContext,
    ) -> api_pb2.FunctionMapResponse:
        return api_pb2.FunctionMapResponse(function_call_id="fc-out")

    async def FunctionPutInputs(
        self,
        request: api_pb2.FunctionPutInputsRequest,
        context: ServicerContext,
    ) -> api_pb2.FunctionPutInputsResponse:
        for function_input in request.inputs:
            args, kwargs = cloudpickle.loads(function_input.args) if function_input.args else ((), {})
            self.client_calls.append((args, kwargs))
        return api_pb2.FunctionPutInputsResponse(status=api_pb2.WRITE_STATUS_SUCCESS)

    async def FunctionGetOutputs(
        self,
        request: api_pb2.FunctionGetOutputsRequest,
        context: ServicerContext,
    ) -> api_pb2.FunctionGetOutputsResponse:
        if self.client_calls:
            args, kwargs = self.client_calls.pop(0)
            # Just return the sum of squares of all args
            res = sum(arg**2 for arg in args) + sum(value**2 for key, value in kwargs.items())
            result = api_pb2.GenericResult(
                status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS,
                data=cloudpickle.dumps(res),
            )
            return api_pb2.FunctionGetOutputsResponse(outputs=[result])
        else:
            await context.abort(StatusCode.DEADLINE_EXCEEDED, "Read timeout")

    async def SecretCreate(
        self,
        request: api_pb2.SecretCreateRequest,
        context: ServicerContext,
    ) -> api_pb2.SecretCreateResponse:
        return api_pb2.SecretCreateResponse(secret_id="st-123")


@pytest.fixture(scope="function")
async def servicer():
    servicer = GRPCClientServicer()
    server = None

    async def _start_servicer():
        nonlocal server
        server = grpc.aio.server()
        api_pb2_grpc.add_ModalClientServicer_to_server(servicer, server)
        port = server.add_insecure_port("[::]:0")
        servicer.remote_addr = "http://localhost:%d" % port
        await server.start()

    async def _stop_servicer():
        await server.stop(0)

    _, aio_start_servicer = synchronize_apis(_start_servicer)
    _, aio_stop_servicer = synchronize_apis(_stop_servicer)

    await aio_start_servicer()
    yield servicer
    await aio_stop_servicer()


@pytest.fixture(scope="function")
async def aio_client(servicer):
    async with AioClient(servicer.remote_addr, api_pb2.CLIENT_TYPE_CLIENT, ("foo-id", "foo-secret")) as client:
        yield client


@pytest.fixture(scope="function")
async def client(servicer):
    with Client(servicer.remote_addr, api_pb2.CLIENT_TYPE_CLIENT, ("foo-id", "foo-secret")) as client:
        yield client


@pytest.fixture(scope="function")
async def aio_container_client(servicer):
    async with AioClient(servicer.remote_addr, api_pb2.CLIENT_TYPE_CONTAINER, ("ta-123", "task-secret")) as client:
        yield client


@pytest.fixture
def reset_global_apps():
    yield
    set_container_app(None)


@pytest.fixture(name="mock_dir", scope="session")
def mock_dir_factory():
    """Sets up a temp dir with content as specified in a nested dict

    Example usage:
    spec = {
        "foo": {
            "bar.txt": "some content"
        },
    }

    with mock_dir(spec) as root_dir:
        assert os.path.exists(os.path.join(root_dir, "foo", "bar.txt"))
    """

    @contextlib.contextmanager
    def mock_dir(root_spec):
        def rec_make(dir, dir_spec):
            for filename, spec in dir_spec.items():
                path = os.path.join(dir, filename)
                if isinstance(spec, str):
                    with open(path, "w") as f:
                        f.write(spec)
                else:
                    os.mkdir(path)
                    rec_make(path, spec)

        # Windows has issues cleaning up TempDirectory: https://www.scivision.dev/python-tempfile-permission-error-windows
        # Seems to have been fixed for some python versions in https://github.com/python/cpython/pull/10320.
        root_dir = tempfile.mkdtemp()
        rec_make(root_dir, root_spec)
        yield root_dir
        shutil.rmtree(root_dir, ignore_errors=True)

    return mock_dir

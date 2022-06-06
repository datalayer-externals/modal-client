import pytest

import modal.exception
from modal import App
from modal.aio import AioApp, AioDebianSlim, AioQueue, AioRunningApp, aio_lookup
from modal.exception import NotFoundError


@pytest.mark.asyncio
async def test_create_object(servicer, aio_client):
    app = AioApp()
    async with app.run(client=aio_client) as running_app:
        q = await AioQueue().create(running_app)
        await q.put("foo")
        await q.put("bar")
        assert await q.get() == "foo"
        assert await q.get() == "bar"


@pytest.mark.asyncio
async def test_persistent_object(servicer, aio_client):
    app_1 = AioApp()
    app_1["q_1"] = AioQueue()
    await app_1.deploy("my-queue", client=aio_client)

    app_2 = AioApp()
    async with app_2.run(client=aio_client) as running_app_2:
        assert isinstance(running_app_2, AioRunningApp)

        with pytest.deprecated_call():
            q_2 = await running_app_2.include("my-queue")
            assert isinstance(q_2, AioQueue)
            assert q_2.object_id == "qu-1"

        q_3 = await aio_lookup("my-queue", client=aio_client)
        assert isinstance(q_3, AioQueue)
        assert q_3.object_id == "qu-1"

        with pytest.raises(NotFoundError):
            await aio_lookup("bazbazbaz", client=aio_client)


def square(x):
    return x**2


@pytest.mark.asyncio
async def test_redeploy(servicer, aio_client):
    app = AioApp()
    app.function(square)
    f_name = "client_test.app_test.square"

    # Deploy app
    app_id = await app.deploy("my-app", client=aio_client)
    assert app_id == "ap-1"
    assert servicer.app_objects["ap-1"][f_name] == "fu-1"

    # Redeploy, make sure all ids are the same
    app_id = await app.deploy("my-app", client=aio_client)
    assert app_id == "ap-1"
    assert servicer.app_objects["ap-1"][f_name] == "fu-1"

    # Deploy to a different name, ids should change
    app_id = await app.deploy("my-app-xyz", client=aio_client)
    assert app_id == "ap-2"
    assert servicer.app_objects["ap-2"][f_name] == "fu-2"


# Should exit without waiting for the logs grace period.
@pytest.mark.timeout(1)
def test_create_object_exception(servicer, client):
    servicer.function_create_error = True

    app = App()

    @app.function
    def f():
        pass

    with pytest.raises(Exception):
        with app.run(client=client):
            pass


def test_deploy_falls_back_to_app_name(servicer, client):
    named_app = App(name="foo_app")
    named_app.deploy(client=client)
    assert "foo_app" in servicer.deployed_apps


def test_deploy_uses_deployment_name_if_specified(servicer, client):
    named_app = App(name="foo_app")
    named_app.deploy("bar_app", client=client)
    assert "bar_app" in servicer.deployed_apps
    assert "foo_app" not in servicer.deployed_apps


@pytest.mark.skip(reason="revisit in a sec once the app state stuff is fixed")
def test_run_function_without_app_error():
    app = App()

    @app.function()
    def foo():
        pass

    with pytest.raises(modal.exception.InvalidError):
        foo()


@pytest.mark.asyncio
async def test_standalone_object(aio_client):
    app = AioApp()
    image = AioDebianSlim()

    @app.function
    def foo(image=image):
        pass

    async with app.run(client=aio_client):
        pass


@pytest.mark.asyncio
async def test_is_inside():
    app = AioApp()
    assert app.is_inside() is False

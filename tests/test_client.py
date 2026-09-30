import httpx
import pytest
import respx

from picnic_mcp import PicnicClient, PicnicCredentials, ResponseError
from picnic_mcp.client import load_operation

GATEWAY = "https://order.trypicnic.com/api/picnic/graphql"


@pytest.fixture
async def client():
    async with PicnicClient(PicnicCredentials(cookie="session=abc")) as c:
        yield c


def test_credentials_to_headers():
    assert PicnicCredentials(token="Bearer xyz").to_headers() == {"authorization": "Bearer xyz"}
    assert PicnicCredentials(cookie="a=b").to_headers() == {"Cookie": "a=b"}


def test_operations_are_packaged():
    assert load_operation("PicnicViewer").lstrip().startswith("query PicnicViewer")


@respx.mock
async def test_get_viewer_sends_operation_and_auth(client):
    route = respx.post(GATEWAY, params={"operation": "PicnicViewer"}).respond(
        json={"data": {"picnicEater": {"id": "eater_1"}}}
    )
    assert await client.get_viewer() == {"id": "eater_1"}
    req = route.calls.last.request
    assert req.headers["cookie"] == "session=abc"
    assert b'"operationName": "PicnicViewer"' in req.content


@respx.mock
async def test_unauthorized_maps_to_401(client):
    respx.post(GATEWAY).respond(401, text='{"error":"Unauthorized"}')
    with pytest.raises(ResponseError) as exc:
        await client.get_viewer()
    assert exc.value.status_code == 401
    assert "Unauthorized" in exc.value.message


@respx.mock
async def test_graphql_errors_on_200_are_raised(client):
    respx.post(GATEWAY).respond(
        json={"data": None, "errors": [{"message": "nope", "extensions": {"code": "FORBIDDEN"}}]}
    )
    with pytest.raises(ResponseError) as exc:
        await client.get_viewer()
    assert exc.value.status_code == 403


@respx.mock
async def test_transport_error_maps_to_503(client):
    respx.post(GATEWAY).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ResponseError) as exc:
        await client.get_viewer()
    assert exc.value.status_code == 503


@respx.mock
async def test_invalid_json_maps_to_503(client):
    respx.post(GATEWAY).respond(200, text="{not json", headers={"content-type": "application/json"})
    with pytest.raises(ResponseError) as exc:
        await client.get_viewer()
    assert exc.value.status_code == 503


async def test_endpoint_must_belong_to_dependency(client):
    with pytest.raises(ValueError):
        await client.post("/graphql", {}, endpoint="acme/getUser")


@respx.mock
async def test_rotated_session_cookie_is_persisted():
    saved = []
    creds = PicnicCredentials(cookie="_oauth2_proxy=old; other=1")
    async with PicnicClient(creds, on_credentials_rotated=saved.append) as client:
        respx.post(GATEWAY).respond(
            json={"data": {"picnicEater": {"id": "e"}}},
            headers={"set-cookie": "_oauth2_proxy=new; Path=/; HttpOnly"},
        )
        await client.get_viewer()
    assert saved[0].cookie == "other=1; _oauth2_proxy=new"


def test_session_issued_at_parses_oauth2_proxy_cookie():
    creds = PicnicCredentials(cookie="a=1; _oauth2_proxy=abc==|1790802716|sig=")
    assert creds.session_issued_at.timestamp() == 1790802716
    assert PicnicCredentials(token="t").session_issued_at is None


@respx.mock
async def test_refresh_session_captures_rotation():
    saved = []
    creds = PicnicCredentials(cookie="_oauth2_proxy=old|1|s")
    async with PicnicClient(creds, on_credentials_rotated=saved.append) as client:
        respx.get("https://order.trypicnic.com/api/iam/v0/session/info").respond(
            json={"accountId": "acct"},
            headers={"set-cookie": "_oauth2_proxy=new|2|s; Path=/"},
        )
        assert (await client.refresh_session())["accountId"] == "acct"
        assert client.credentials.session_issued_at.timestamp() == 2
    assert saved[0].cookie == "_oauth2_proxy=new|2|s"


@respx.mock
async def test_login_flow_captures_session_cookie():
    from picnic_mcp.login import LoginError, start_login, verify_login

    origin = "https://order.trypicnic.com"
    respx.get(f"{origin}/api/oauth2/start").respond(302, headers={"location": f"{origin}/otp-login?x=1"})
    respx.get(f"{origin}/otp-login").respond(200, text="<html/>")
    verify = respx.post(f"{origin}/api/iam/signin/oidc/otp")
    verify.side_effect = [
        httpx.Response(401),
        httpx.Response(302, headers={"location": f"{origin}/api/oauth2/callback"}),
    ]
    respx.get(f"{origin}/api/oauth2/callback").respond(
        302, headers={"location": f"{origin}/", "set-cookie": "_oauth2_proxy=sess|1|sig; Path=/"})
    respx.get(f"{origin}/").respond(200)

    pending = await start_login("me@example.com")
    with pytest.raises(LoginError):
        await verify_login(pending, "000000")
    creds = await verify_login(pending, " 123456 ")
    assert creds.cookie == "_oauth2_proxy=sess|1|sig"
    assert b'"otp":"123456"' in verify.calls.last.request.content

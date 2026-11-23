"""HTTP 层的离线单测：协议级重试、query/body 位置、异常归类。"""
import io
import json
import unittest
from unittest import mock

import httpx

from tsn import http
from tsn import protocol
from tsn.protocol import ResultKind


def _json(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode(), headers={'content-type': 'application/json'})


class CoreRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        http.TsnHttpCore._sleep = staticmethod(lambda attempt: _noop())

    def _core(self, handler, retries=2):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return http.TsnHttpCore(base_url='http://a.example.com', client=client, protocol_retries=retries)

    async def test_degraded_then_ok_retries_and_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            if len(calls) == 1:
                return _json({'code': 1, 'msg': 'Internal Server Error'})
            return _json({'code': 0, 'data': {'ok': True}})

        result = await self._core(handler).send('GET', 'upms/x')
        self.assertIs(result.kind, ResultKind.OK)
        self.assertEqual(len(calls), 2)

    async def test_anti_cheat_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(999, content=b'{"code":999,"msg":"\\u7981\\u8dd1\\u9ed1\\u540d\\u5355"}')

        result = await self._core(handler, retries=5).send('GET', 'upms/x')
        self.assertIs(result.kind, ResultKind.ANTI_CHEAT)
        self.assertEqual(len(calls), 1)

    async def test_transport_error_is_classified(self):
        def handler(request):
            raise httpx.ConnectError('boom', request=request)

        result = await self._core(handler, retries=1).send('GET', 'upms/x')
        self.assertIs(result.kind, ResultKind.TRANSPORT)

    async def test_url_is_joined_with_separator(self):
        seen = {}

        def handler(request):
            seen['url'] = str(request.url)
            return _json({'code': 0, 'data': 1})

        await self._core(handler).send('GET', 'upms/basUser/getLoginUserInfo')
        self.assertEqual(seen['url'], 'http://a.example.com/upms/basUser/getLoginUserInfo')


class OAuthTransport(unittest.IsolatedAsyncioTestCase):
    """令牌端点必须走 query string —— 表单体会被服务端拒绝（428/验证码不能为空）。"""

    async def test_access_token_sends_params_in_query_not_body(self):
        from sdk.public import TiShiNengSdkPublic

        captured = {}

        def handler(request):
            captured['url'] = str(request.url)
            captured['body'] = request.content
            captured['auth'] = request.headers.get('authorization', '')
            return _json({'access_token': 'tok', 'refresh_token': 'ref', 'expires_in': 86399})

        http.TsnHttpCore._sleep = staticmethod(lambda attempt: _noop())
        sdk = TiShiNengSdkPublic(0, 1, 'demo', 'openid', 'dev', 'Xiaomi', 'num', 'android', '', '')
        sdk.setCloudUrl('http://a.example.com')
        sdk.core.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        payload = await sdk.getAccessToken('u123', 'p456')

        self.assertEqual(payload['access_token'], 'tok')
        self.assertIn('username=u123', captured['url'])
        self.assertIn('type=app', captured['url'])
        self.assertEqual(captured['body'], b'')
        self.assertTrue(captured['auth'].startswith('Basic '))



class MultipartContentType(unittest.IsolatedAsyncioTestCase):
    """multipart 上传不能被客户端默认的 form-urlencoded Content-Type 顶掉。

    真实事故：exerciseRunningFace 的 body 里混着 JPEG 字节，服务端拿到
    `Content-Type: application/x-www-form-urlencoded` 后把整段 body 当表单用
    URLDecoder 解析，遇到图片里的 0x25 直接 500：
    `Illegal hex characters in escape (%) pattern`。
    """

    def _core(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return http.TsnHttpCore(
            base_url='http://a.example.com',
            client=client,
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'token': 't'},
        )

    async def test_files_switches_to_multipart_boundary(self):
        seen = {}

        def handler(request):
            seen['content-type'] = request.headers.get('content-type', '')
            seen['body'] = request.content
            return _json({'code': 0, 'data': True})

        image = b'\xff\xd8\xff\xdb%SD)\x00'
        result = await self._core(handler).send(
            'POST',
            'exercise/exerciseRunningFace/1/face',
            data={'key': 'k', 'param': 'p'},
            files={'file': ('file.jpg', io.BytesIO(image), 'image/jpeg')},
        )

        self.assertIs(result.kind, ResultKind.OK)
        self.assertTrue(seen['content-type'].startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="file"', seen['body'])
        self.assertIn(b'%SD)', seen['body'])

    async def test_plain_form_keeps_default_content_type(self):
        seen = {}

        def handler(request):
            seen['content-type'] = request.headers.get('content-type', '')
            return _json({'code': 0, 'data': True})

        await self._core(handler).send('POST', 'exercise/x', data={'a': '1'})
        self.assertEqual(seen['content-type'], 'application/x-www-form-urlencoded')

async def _noop():
    return None


if __name__ == '__main__':
    unittest.main()

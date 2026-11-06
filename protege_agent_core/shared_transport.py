import httpx

class _SharedTransport(httpx.AsyncHTTPTransport):
    async def __aexit__(self, *args): return None 
    async def aclose(self): return None
    async def close_pool(self): await super().aclose()

_transport = _SharedTransport(
    verify=False,  
    http2=True,
    limits=httpx.Limits(max_connections=300, max_keepalive_connections=50, keepalive_expiry=120),
)

def pooled_client_factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport,
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
    )
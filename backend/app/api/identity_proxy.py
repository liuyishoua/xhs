"""Compatibility for existing /api/auth clients; no local identity writes."""
from fastapi import APIRouter, Request, Response
import httpx
from backend.app.core.ops_identity import endpoint
router = APIRouter(prefix='/auth')
@router.api_route('/{path:path}', methods=['GET', 'POST'])
async def forward(path: str, request: Request):
    if path not in {'login','register','refresh','logout','me','workspace/session','workspace/exchange','workspace/verify','workspace/logout','validate'}:
        return Response(status_code=404)
    headers = {key: request.headers[key] for key in ['authorization','cookie','origin','x-app-request','content-type'] if key in request.headers}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            result = await client.request(request.method, endpoint() + '/api/auth/' + path, headers=headers, content=await request.body())
    except httpx.HTTPError:
        return Response('Identity service unavailable',status_code=503)
    response = Response(result.content, status_code=result.status_code, headers={'Content-Type':result.headers.get('content-type','application/json'),'Cache-Control':'private, no-store'})
    for value in result.headers.get_list('set-cookie'): response.headers.append('set-cookie', value)
    return response

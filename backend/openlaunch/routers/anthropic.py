from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from openlaunch.constants import ERROR_MESSAGES
from openlaunch.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT,
)
from openlaunch.events import EVENTS, publish_event, publish_model_provider_request_failed
from openlaunch.models.config import Config
from openlaunch.models.models import Models
from openlaunch.models.users import UserModel
from openlaunch.utils.access_control import check_model_access
from openlaunch.utils.anthropic import (
    anthropic_stream_to_openai_stream,
    convert_anthropic_error_to_openai,
    convert_anthropic_to_openai_response,
    convert_openai_to_anthropic_payload,
    get_anthropic_headers,
    get_anthropic_models,
    normalize_anthropic_base_url,
)
from openlaunch.utils.auth import get_admin_user, get_verified_user
from openlaunch.utils.payload import apply_model_params_to_body_openai, apply_system_prompt_to_body
from openlaunch.utils.session_pool import cleanup_response, get_session, stream_wrapper
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter()

_STRIP_PROXY_HEADERS = frozenset({'Content-Encoding', 'Content-Length', 'Transfer-Encoding'})


def _clean_proxy_headers(raw_headers) -> dict:
    return {key: value for key, value in raw_headers.items() if key not in _STRIP_PROXY_HEADERS}


ANTHROPIC_CONFIG_KEYS = {
    'ENABLE_ANTHROPIC_API': 'anthropic.enable',
    'ANTHROPIC_API_BASE_URLS': 'anthropic.api_base_urls',
    'ANTHROPIC_API_KEYS': 'anthropic.api_keys',
    'ANTHROPIC_API_CONFIGS': 'anthropic.api_configs',
}


async def get_anthropic_config() -> dict:
    values = await Config.get_many(*ANTHROPIC_CONFIG_KEYS.values())
    return {field: values[storage_key] for field, storage_key in ANTHROPIC_CONFIG_KEYS.items() if storage_key in values}


async def get_anthropic_runtime_config() -> tuple[bool, list[str], list[str], dict]:
    values = await Config.get_many(*ANTHROPIC_CONFIG_KEYS.values())
    urls = [normalize_anthropic_base_url(url) for url in values.get('anthropic.api_base_urls') or []]
    keys = values.get('anthropic.api_keys') or []
    if len(keys) < len(urls):
        keys = [*keys, *([''] * (len(urls) - len(keys)))]
    elif len(keys) > len(urls):
        keys = keys[: len(urls)]
    return (
        bool(values.get('anthropic.enable')),
        urls,
        keys,
        values.get('anthropic.api_configs') or {},
    )


def resolve_api_config(api_configs: dict, idx: int, url: str) -> dict:
    return api_configs.get(str(idx), api_configs.get(url, {}))


async def get_anthropic_connection(idx: int) -> tuple[str, str, dict]:
    _, urls, keys, configs = await get_anthropic_runtime_config()
    try:
        url = urls[idx]
        key = keys[idx]
    except IndexError as exc:
        raise HTTPException(status_code=404, detail='Anthropic connection not found') from exc
    return url, key, resolve_api_config(configs, idx, url)


@router.get('/config')
async def get_config(user=Depends(get_admin_user)):
    return await get_anthropic_config()


class AnthropicConfigForm(BaseModel):
    ENABLE_ANTHROPIC_API: bool | None = None
    ANTHROPIC_API_BASE_URLS: list[str]
    ANTHROPIC_API_KEYS: list[str]
    ANTHROPIC_API_CONFIGS: dict


@router.post('/config/update')
async def update_config(request: Request, form_data: AnthropicConfigForm, user=Depends(get_admin_user)):
    urls = [normalize_anthropic_base_url(url) for url in form_data.ANTHROPIC_API_BASE_URLS]
    keys = form_data.ANTHROPIC_API_KEYS[: len(urls)]
    keys.extend([''] * (len(urls) - len(keys)))
    valid_keys = set(map(str, range(len(urls))))
    configs = {key: value for key, value in form_data.ANTHROPIC_API_CONFIGS.items() if key in valid_keys}

    await Config.upsert(
        {
            'anthropic.enable': form_data.ENABLE_ANTHROPIC_API,
            'anthropic.api_base_urls': urls,
            'anthropic.api_keys': keys,
            'anthropic.api_configs': configs,
        }
    )
    request.app.state.ANTHROPIC_MODELS = {}
    request.app.state.BASE_MODELS = []
    request.app.state.MODELS = {}
    await publish_event(
        request,
        EVENTS.MODEL_PROVIDER_CONFIG_UPDATED,
        actor=user,
        subject_id='anthropic',
        subject_type='model.provider_config',
        data={
            'provider': 'anthropic',
            'enabled': form_data.ENABLE_ANTHROPIC_API,
            'base_url_count': len(urls),
        },
    )
    return {
        'ENABLE_ANTHROPIC_API': form_data.ENABLE_ANTHROPIC_API,
        'ANTHROPIC_API_BASE_URLS': urls,
        'ANTHROPIC_API_KEYS': keys,
        'ANTHROPIC_API_CONFIGS': configs,
    }


async def _get_models_for_connection(url: str, key: str, config: dict, user: UserModel | None):
    model_ids = config.get('model_ids') or []
    if model_ids:
        return {
            'object': 'list',
            'data': [
                {
                    'id': model_id,
                    'name': model_id,
                    'object': 'model',
                    'owned_by': 'anthropic',
                }
                for model_id in model_ids
            ],
        }
    return await get_anthropic_models(url, key, user=user, config=config)


async def get_all_models(request: Request, user: UserModel | None = None) -> dict:
    enabled, urls, keys, configs = await get_anthropic_runtime_config()
    if not enabled:
        request.app.state.ANTHROPIC_MODELS = {}
        return {'data': []}

    tasks = []
    active_indices = []
    for idx, url in enumerate(urls):
        config = resolve_api_config(configs, idx, url)
        if not config.get('enable', True):
            continue
        active_indices.append(idx)
        tasks.append(_get_models_for_connection(url, keys[idx], config, user))

    responses = await asyncio.gather(*tasks) if tasks else []
    models = {}
    for idx, response in zip(active_indices, responses):
        if not response or response.get('error'):
            continue
        url = urls[idx]
        config = resolve_api_config(configs, idx, url)
        prefix_id = config.get('prefix_id')
        for raw_model in response.get('data') or []:
            source_id = raw_model.get('id') or raw_model.get('name')
            if not source_id:
                continue
            model_id = f'{prefix_id}.{source_id}' if prefix_id else source_id
            model = {
                **raw_model,
                'id': model_id,
                'name': raw_model.get('name') or source_id,
                'owned_by': 'anthropic',
                'anthropic': raw_model.get('anthropic', raw_model),
                'provider': 'anthropic',
                'connection_type': config.get('connection_type', 'external'),
                'urlIdx': idx,
            }
            if config.get('tags'):
                model['tags'] = config['tags']
            models.setdefault(model_id, model)

    request.app.state.ANTHROPIC_MODELS = models
    return {'data': list(models.values())}


@router.get('/models')
@router.get('/models/{url_idx}')
async def get_models(request: Request, url_idx: int | None = None, user=Depends(get_verified_user)):
    if not await Config.get('anthropic.enable'):
        raise HTTPException(status_code=503, detail='Anthropic API is disabled')
    if url_idx is None:
        return await get_all_models(request, user=user)
    url, key, config = await get_anthropic_connection(url_idx)
    result = await _get_models_for_connection(url, key, config, user)
    if result.get('error'):
        error = convert_anthropic_error_to_openai(result['error'], result.get('request_id'))
        raise HTTPException(status_code=result.get('status', 502), detail=error['error'])
    return result


class ConnectionVerificationForm(BaseModel):
    url: str | None = None
    key: str
    config: dict | None = None


@router.post('/verify')
async def verify_connection(form_data: ConnectionVerificationForm, user=Depends(get_admin_user)):
    url = normalize_anthropic_base_url(form_data.url)
    result = await get_anthropic_models(url, form_data.key, user=user, config=form_data.config or {})
    if result.get('error'):
        error = convert_anthropic_error_to_openai(result['error'], result.get('request_id'))
        return JSONResponse(status_code=result.get('status', 502), content=error)
    return result


async def request_anthropic_chat_completion(
    request: Request,
    payload: dict,
    user: UserModel,
    url: str,
    key: str,
    config: dict | None = None,
):
    """Send an OpenAI-shaped payload to a native Anthropic-compatible endpoint."""
    config = config or {}
    url = normalize_anthropic_base_url(url)
    requested_model = payload.get('model')
    native_payload = convert_openai_to_anthropic_payload(
        payload,
        default_max_tokens=int(config.get('max_tokens') or 4096),
    )
    headers = get_anthropic_headers(key, config, user)
    response = None
    streaming = False

    try:
        session = await get_session()
        response = await session.post(
            f'{url}/messages',
            data=json.dumps(native_payload),
            headers=headers,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        )
        request_id = response.headers.get('request-id')
        if response.status >= 400:
            try:
                upstream_error = await response.json()
            except Exception:
                upstream_error = await response.text()
            await publish_model_provider_request_failed(
                request,
                actor=user,
                provider='anthropic',
                base_url=url,
                api_key=key,
                status=response.status,
                requested_model=requested_model,
                upstream_error=upstream_error,
            )
            content = convert_anthropic_error_to_openai(upstream_error, request_id)
            return JSONResponse(
                status_code=response.status,
                content=content,
                headers={'request-id': request_id} if request_id else None,
            )

        if 'text/event-stream' in response.headers.get('Content-Type', '') or native_payload.get('stream'):
            streaming = True
            return StreamingResponse(
                anthropic_stream_to_openai_stream(stream_wrapper(response), model=requested_model),
                status_code=response.status,
                media_type='text/event-stream',
                headers=_clean_proxy_headers(response.headers),
            )

        try:
            result = await response.json()
        except Exception:
            body = await response.text()
            return PlainTextResponse(status_code=502, content=body)
        return convert_anthropic_to_openai_response(result, request_id=request_id)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('Anthropic provider request failed')
        raise HTTPException(status_code=502, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR) from exc
    finally:
        if not streaming:
            await cleanup_response(response)


@router.post('/chat/completions')
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    bypass_filter = getattr(request.state, 'bypass_filter', False)
    bypass_system_prompt = getattr(request.state, 'bypass_system_prompt', False)
    payload = {**form_data}
    metadata = payload.pop('metadata', None)
    model_id = form_data.get('model')
    model_info = await Models.get_model_by_id(model_id)

    if model_info:
        if model_info.base_model_id:
            payload['model'] = model_info.base_model_id
            model_id = model_info.base_model_id
        params = model_info.params.model_dump()
        if params:
            system = params.pop('system', None)
            payload = apply_model_params_to_body_openai(params, payload)
            if not bypass_system_prompt:
                payload = await apply_system_prompt_to_body(system, payload, metadata, user)
        await check_model_access(user, model_info, bypass_filter)
    else:
        await check_model_access(user, None, bypass_filter)

    models = request.app.state.ANTHROPIC_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.ANTHROPIC_MODELS
    model = models.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.MODEL_NOT_FOUND())

    url, key, config = await get_anthropic_connection(model['urlIdx'])
    prefix_id = config.get('prefix_id')
    if prefix_id and payload.get('model', '').startswith(f'{prefix_id}.'):
        payload['model'] = payload['model'][len(prefix_id) + 1 :]

    return await request_anthropic_chat_completion(request, payload, user, url, key, config)

import datetime as dt
import json
import logging
import time
import uuid

import aiohttp
from openlaunch.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    ENABLE_FORWARD_USER_INFO_HEADERS,
)
from openlaunch.models.users import UserModel
from openlaunch.utils.headers import include_user_info_headers

log = logging.getLogger(__name__)


def is_anthropic_url(url: str) -> bool:
    """Check if the URL is an Anthropic API endpoint."""
    return 'api.anthropic.com' in url


def normalize_anthropic_base_url(url: str | None) -> str:
    """Normalize a Messages API base URL without guessing compatible paths."""
    normalized = (url or 'https://api.anthropic.com/v1').strip().rstrip('/')
    if normalized == 'https://api.anthropic.com':
        return f'{normalized}/v1'
    return normalized


def get_anthropic_headers(
    key: str = '',
    config: dict | None = None,
    user: UserModel | None = None,
) -> dict:
    """Build native Anthropic headers, with compatible auth and custom-header support."""
    config = config or {}
    headers = {
        'Content-Type': 'application/json',
        'anthropic-version': config.get('anthropic_version') or '2023-06-01',
    }
    auth_type = config.get('auth_type', 'api_key')
    if key and auth_type == 'bearer':
        headers['Authorization'] = f'Bearer {key}'
    elif key and auth_type not in ('none',):
        headers['x-api-key'] = key

    if ENABLE_FORWARD_USER_INFO_HEADERS and user:
        headers = include_user_info_headers(headers, user)

    if isinstance(config.get('headers'), dict):
        headers.update({str(name): str(value) for name, value in config['headers'].items()})
    return headers


async def get_anthropic_models(
    url: str,
    key: str,
    user: UserModel = None,
    config: dict | None = None,
) -> dict:
    """
    Fetch models from Anthropic's /v1/models endpoint with pagination.
    Normalizes the response to OpenAI format.
    """
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    all_models = []
    after_id = None

    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            headers = get_anthropic_headers(key, config, user)

            while True:
                params = {'limit': 1000}
                if after_id:
                    params['after_id'] = after_id

                async with session.get(
                    f'{normalize_anthropic_base_url(url)}/models',
                    headers=headers,
                    params=params,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as response:
                    if response.status != 200:
                        error_detail = f'HTTP Error: {response.status}'
                        try:
                            res = await response.json()
                            if 'error' in res:
                                error_detail = res['error']
                        except Exception:
                            pass
                        return {
                            'object': 'list',
                            'data': [],
                            'error': error_detail,
                            'status': response.status,
                            'request_id': response.headers.get('request-id'),
                        }

                    data = await response.json()

                    for model in data.get('data', []):
                        all_models.append(
                            {
                                'id': model.get('id'),
                                'object': 'model',
                                'created': _anthropic_timestamp(model.get('created_at')),
                                'owned_by': 'anthropic',
                                'name': model.get('display_name', model.get('id')),
                                'anthropic': model,
                            }
                        )

                    if not data.get('has_more', False):
                        break
                    after_id = data.get('last_id')

    except Exception as e:
        log.error(f'Anthropic connection error: {e}')
        return {'object': 'list', 'data': [], 'error': {'message': str(e)}}

    return {'object': 'list', 'data': all_models}


def _anthropic_timestamp(value) -> int:
    if not value:
        return 0
    try:
        return int(dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp())
    except (TypeError, ValueError):
        return 0


##############################
#
# Anthropic Messages API Conversion Utilities
#
##############################


def _copy_cache_control(source: dict, target: dict) -> dict:
    if isinstance(source, dict) and 'cache_control' in source:
        target['cache_control'] = source['cache_control']
    return target


def _has_cache_control(blocks: list) -> bool:
    return any(isinstance(block, dict) and 'cache_control' in block for block in blocks)


def _finalize_openai_content(blocks: list) -> str | list:
    if not blocks:
        return ''

    if len(blocks) == 1 and blocks[0].get('type') == 'text' and not _has_cache_control(blocks):
        return blocks[0].get('text', '')

    return blocks


def _openai_part_to_anthropic(part) -> dict | None:
    if isinstance(part, str):
        return {'type': 'text', 'text': part}
    if not isinstance(part, dict):
        return None

    part_type = part.get('type', 'text')
    if part_type in ('text', 'input_text'):
        return _copy_cache_control(part, {'type': 'text', 'text': part.get('text', '')})
    if part_type in ('image_url', 'input_image'):
        image = part.get('image_url', part.get('image', ''))
        image_url = image.get('url', '') if isinstance(image, dict) else image
        if not isinstance(image_url, str):
            return None
        if image_url.startswith('data:') and ';base64,' in image_url:
            header, data = image_url.split(';base64,', 1)
            return _copy_cache_control(
                part,
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': header[5:] or 'image/png',
                        'data': data,
                    },
                },
            )
        return _copy_cache_control(
            part,
            {'type': 'image', 'source': {'type': 'url', 'url': image_url}},
        )
    return None


def _openai_content_to_anthropic(content) -> str | list:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return '' if content is None else str(content)
    blocks = []
    for part in content:
        block = _openai_part_to_anthropic(part)
        if block is not None:
            blocks.append(block)
    return blocks


def convert_openai_to_anthropic_payload(openai_payload: dict, default_max_tokens: int = 4096) -> dict:
    """Convert OpenLaunch's OpenAI-shaped request into a native Messages request."""
    max_tokens = openai_payload.get('max_completion_tokens') or openai_payload.get('max_tokens') or default_max_tokens
    payload = {
        'model': openai_payload.get('model', ''),
        'max_tokens': max_tokens,
        'messages': [],
    }

    system_blocks = []
    for message in openai_payload.get('messages', []):
        role = message.get('role', 'user')
        content = message.get('content', '')

        if role in ('system', 'developer'):
            converted = _openai_content_to_anthropic(content)
            if isinstance(converted, str):
                system_blocks.append({'type': 'text', 'text': converted})
            else:
                system_blocks.extend(block for block in converted if block.get('type') == 'text')
            continue

        if role == 'assistant':
            blocks = []
            converted = _openai_content_to_anthropic(content)
            if isinstance(converted, str):
                if converted:
                    blocks.append({'type': 'text', 'text': converted})
            else:
                blocks.extend(converted)
            for tool_call in message.get('tool_calls') or []:
                function = tool_call.get('function') or {}
                arguments = function.get('arguments', {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, TypeError):
                        arguments = {'value': arguments}
                blocks.append(
                    {
                        'type': 'tool_use',
                        'id': tool_call.get('id') or f'toolu_{uuid.uuid4().hex[:24]}',
                        'name': function.get('name', ''),
                        'input': arguments if isinstance(arguments, dict) else {},
                    }
                )
            payload['messages'].append({'role': 'assistant', 'content': blocks or ''})
            continue

        if role == 'tool':
            tool_content = _openai_content_to_anthropic(content)
            payload['messages'].append(
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'tool_result',
                            'tool_use_id': message.get('tool_call_id', ''),
                            'content': tool_content,
                            **({'is_error': True} if message.get('is_error') else {}),
                        }
                    ],
                }
            )
            continue

        payload['messages'].append(
            {
                'role': 'user',
                'content': _openai_content_to_anthropic(content),
            }
        )

    if system_blocks:
        payload['system'] = system_blocks

    param_map = {
        'temperature': 'temperature',
        'top_p': 'top_p',
        'top_k': 'top_k',
        'stream': 'stream',
        'stop': 'stop_sequences',
        'service_tier': 'service_tier',
        'thinking': 'thinking',
    }
    for source, target in param_map.items():
        if source in openai_payload and openai_payload[source] is not None:
            payload[target] = openai_payload[source]

    metadata = openai_payload.get('metadata')
    if isinstance(metadata, dict) and metadata.get('user_id'):
        payload['metadata'] = {'user_id': str(metadata['user_id'])}

    tools = []
    for tool in openai_payload.get('tools') or []:
        function = tool.get('function', tool) if isinstance(tool, dict) else {}
        if not isinstance(function, dict) or not function.get('name'):
            continue
        converted = {
            'name': function['name'],
            'input_schema': function.get('parameters') or function.get('input_schema') or {'type': 'object'},
        }
        if function.get('description'):
            converted['description'] = function['description']
        _copy_cache_control(tool, converted)
        tools.append(converted)
    if tools:
        payload['tools'] = tools

    tool_choice = openai_payload.get('tool_choice')
    if tool_choice:
        if tool_choice == 'auto':
            payload['tool_choice'] = {'type': 'auto'}
        elif tool_choice in ('required', 'any'):
            payload['tool_choice'] = {'type': 'any'}
        elif tool_choice == 'none':
            pass
        elif isinstance(tool_choice, dict):
            function = tool_choice.get('function') or {}
            if function.get('name'):
                payload['tool_choice'] = {'type': 'tool', 'name': function['name']}

    return payload


def convert_anthropic_usage_to_openai(usage: dict | None) -> dict:
    """Preserve Anthropic accounting while adding OpenAI-compatible totals."""
    usage = dict(usage or {})
    uncached = int(usage.get('input_tokens') or 0)
    cache_creation = int(usage.get('cache_creation_input_tokens') or 0)
    cache_read = int(usage.get('cache_read_input_tokens') or 0)
    prompt_tokens = uncached + cache_creation + cache_read
    completion_tokens = int(usage.get('output_tokens') or 0)
    usage.update(
        {
            'anthropic_input_tokens': uncached,
            'anthropic_output_tokens': completion_tokens,
            'input_tokens': prompt_tokens,
            'output_tokens': completion_tokens,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': prompt_tokens + completion_tokens,
        }
    )
    if cache_read:
        details = dict(usage.get('prompt_tokens_details') or {})
        details['cached_tokens'] = cache_read
        usage['prompt_tokens_details'] = details
    return usage


def convert_anthropic_to_openai_response(response: dict, request_id: str | None = None) -> dict:
    """Convert a native Messages response into Chat Completions format."""
    text_parts = []
    tool_calls = []
    extra_blocks = []
    for block in response.get('content') or []:
        block_type = block.get('type')
        if block_type == 'text':
            text_parts.append(block.get('text', ''))
        elif block_type == 'tool_use':
            tool_calls.append(
                {
                    'id': block.get('id') or f'call_{uuid.uuid4().hex}',
                    'type': 'function',
                    'function': {
                        'name': block.get('name', ''),
                        'arguments': json.dumps(block.get('input') or {}, separators=(',', ':')),
                    },
                }
            )
        else:
            extra_blocks.append(block)

    stop_reason_map = {
        'end_turn': 'stop',
        'stop_sequence': 'stop',
        'max_tokens': 'length',
        'tool_use': 'tool_calls',
        'pause_turn': 'stop',
        'refusal': 'content_filter',
    }
    message = {'role': 'assistant', 'content': ''.join(text_parts)}
    if tool_calls:
        message['tool_calls'] = tool_calls
    if extra_blocks:
        message['anthropic_content'] = extra_blocks

    result = {
        'id': response.get('id') or f'chatcmpl-{uuid.uuid4()}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': response.get('model', ''),
        'choices': [
            {
                'index': 0,
                'message': message,
                'finish_reason': stop_reason_map.get(response.get('stop_reason'), 'stop'),
            }
        ],
        'usage': convert_anthropic_usage_to_openai(response.get('usage')),
    }
    if request_id:
        result['request_id'] = request_id
    if response.get('stop_sequence') is not None:
        result['stop_sequence'] = response['stop_sequence']
    return result


def convert_anthropic_error_to_openai(error: dict | str, request_id: str | None = None) -> dict:
    """Keep the provider's typed error while exposing the shape OpenAI clients expect."""
    if isinstance(error, dict):
        source = error.get('error', error)
        message = source.get('message', str(source)) if isinstance(source, dict) else str(source)
        error_type = source.get('type', 'api_error') if isinstance(source, dict) else 'api_error'
        result = {'error': {'message': message, 'type': error_type, 'code': error_type}}
        request_id = request_id or error.get('request_id')
    else:
        result = {'error': {'message': str(error), 'type': 'api_error', 'code': 'api_error'}}
    if request_id:
        result['request_id'] = request_id
    return result


async def anthropic_stream_to_openai_stream(anthropic_stream, model: str = ''):
    """Translate native named SSE events into OpenAI Chat Completions SSE chunks."""
    completion_id = f'chatcmpl-{uuid.uuid4()}'
    created = int(time.time())
    buffer = ''
    current_event = None
    tool_indices = {}
    usage = {}

    def chunk(delta=None, finish_reason=None, chunk_usage=None):
        data = {
            'id': completion_id,
            'object': 'chat.completion.chunk',
            'created': created,
            'model': model,
            'choices': [
                {
                    'index': 0,
                    'delta': delta or {},
                    'finish_reason': finish_reason,
                }
            ],
        }
        if chunk_usage is not None:
            data['usage'] = chunk_usage
        return f'data: {json.dumps(data)}\n\n'.encode()

    async def handle_event(event_name, raw_data):
        nonlocal model, usage
        try:
            data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return []
        event_type = data.get('type') or event_name
        emitted = []
        if event_type == 'message_start':
            message = data.get('message') or {}
            model = message.get('model') or model
            usage.update(message.get('usage') or {})
            emitted.append(chunk({'role': 'assistant'}))
        elif event_type == 'content_block_start':
            block = data.get('content_block') or {}
            if block.get('type') == 'text' and block.get('text'):
                emitted.append(chunk({'content': block['text']}))
            elif block.get('type') == 'tool_use':
                index = int(data.get('index', 0))
                tool_indices[index] = len(tool_indices)
                emitted.append(
                    chunk(
                        {
                            'tool_calls': [
                                {
                                    'index': tool_indices[index],
                                    'id': block.get('id', ''),
                                    'type': 'function',
                                    'function': {'name': block.get('name', ''), 'arguments': ''},
                                }
                            ]
                        }
                    )
                )
        elif event_type == 'content_block_delta':
            delta = data.get('delta') or {}
            if delta.get('type') == 'text_delta':
                emitted.append(chunk({'content': delta.get('text', '')}))
            elif delta.get('type') == 'input_json_delta':
                index = int(data.get('index', 0))
                emitted.append(
                    chunk(
                        {
                            'tool_calls': [
                                {
                                    'index': tool_indices.setdefault(index, len(tool_indices)),
                                    'function': {'arguments': delta.get('partial_json', '')},
                                }
                            ]
                        }
                    )
                )
        elif event_type == 'message_delta':
            usage.update(data.get('usage') or {})
            stop_reason = (data.get('delta') or {}).get('stop_reason')
            finish_map = {
                'end_turn': 'stop',
                'stop_sequence': 'stop',
                'max_tokens': 'length',
                'tool_use': 'tool_calls',
                'pause_turn': 'stop',
                'refusal': 'content_filter',
            }
            emitted.append(
                chunk(
                    {},
                    finish_map.get(stop_reason, 'stop') if stop_reason else None,
                    convert_anthropic_usage_to_openai(usage),
                )
            )
        elif event_type == 'error':
            emitted.append(f'data: {json.dumps(convert_anthropic_error_to_openai(data))}\n\n'.encode())
        return emitted

    async for raw_chunk in anthropic_stream:
        buffer += raw_chunk.decode('utf-8', errors='replace') if isinstance(raw_chunk, bytes) else str(raw_chunk)
        buffer = buffer.replace('\r\n', '\n')
        while '\n\n' in buffer:
            frame, buffer = buffer.split('\n\n', 1)
            event_name = current_event
            data_lines = []
            for line in frame.replace('\r\n', '\n').split('\n'):
                if line.startswith('event:'):
                    event_name = line[6:].strip()
                elif line.startswith('data:'):
                    data_lines.append(line[5:].lstrip())
            current_event = None
            if data_lines:
                for emitted in await handle_event(event_name, '\n'.join(data_lines)):
                    yield emitted

    if buffer.strip():
        data_lines = [line[5:].lstrip() for line in buffer.splitlines() if line.startswith('data:')]
        if data_lines:
            for emitted in await handle_event(current_event, '\n'.join(data_lines)):
                yield emitted
    yield b'data: [DONE]\n\n'


def convert_anthropic_to_openai_payload(anthropic_payload: dict) -> dict:
    """
    Convert an Anthropic Messages API request to OpenAI Chat Completions format.

    Anthropic format:
        {model, messages: [{role, content}], system, max_tokens, ...}
    OpenAI format:
        {model, messages: [{role, content}], max_tokens, ...}
    """
    openai_payload = {}

    # Model
    openai_payload['model'] = anthropic_payload.get('model', '')

    # Build messages list
    messages = []

    # System prompt (Anthropic has it as top-level, OpenAI as a system message)
    system = anthropic_payload.get('system')
    if system:
        if isinstance(system, str):
            messages.append({'role': 'system', 'content': system})
        elif isinstance(system, list):
            openai_content = []
            for block in system:
                if isinstance(block, dict) and block.get('type') == 'text':
                    openai_content.append(
                        _copy_cache_control(
                            block,
                            {
                                'type': 'text',
                                'text': block.get('text', ''),
                            },
                        )
                    )
                elif isinstance(block, str):
                    openai_content.append({'type': 'text', 'text': block})
            messages.append({'role': 'system', 'content': _finalize_openai_content(openai_content)})

    # Convert messages
    for msg in anthropic_payload.get('messages', []):
        role = msg.get('role', 'user')
        content = msg.get('content')

        if isinstance(content, str):
            messages.append({'role': role, 'content': content})
        elif isinstance(content, list):
            # Convert Anthropic content blocks to OpenAI format
            openai_content = []
            tool_calls = []

            for block in content:
                block_type = block.get('type', 'text')

                if block_type == 'text':
                    openai_content.append(
                        _copy_cache_control(
                            block,
                            {
                                'type': 'text',
                                'text': block.get('text', ''),
                            },
                        )
                    )
                elif block_type == 'image':
                    source = block.get('source', {})
                    if source.get('type') == 'base64':
                        media_type = source.get('media_type', 'image/png')
                        data = source.get('data', '')
                        openai_content.append(
                            _copy_cache_control(
                                block,
                                {
                                    'type': 'image_url',
                                    'image_url': {
                                        'url': f'data:{media_type};base64,{data}',
                                    },
                                },
                            )
                        )
                    elif source.get('type') == 'url':
                        openai_content.append(
                            _copy_cache_control(
                                block,
                                {
                                    'type': 'image_url',
                                    'image_url': {'url': source.get('url', '')},
                                },
                            )
                        )
                elif block_type == 'tool_use':
                    tool_calls.append(
                        {
                            'id': block.get('id', ''),
                            'type': 'function',
                            'function': {
                                'name': block.get('name', ''),
                                'arguments': (
                                    json.dumps(block.get('input', {}))
                                    if isinstance(block.get('input'), dict)
                                    else str(block.get('input', '{}'))
                                ),
                            },
                        }
                    )
                elif block_type == 'tool_result':
                    # Tool results become separate tool messages in OpenAI format
                    tool_result_content = block.get('content', '')
                    tool_content: str | list = ''

                    if isinstance(tool_result_content, str):
                        tool_content = tool_result_content
                    elif isinstance(tool_result_content, list):
                        # Build a multimodal content array to preserve
                        # images and other non-text content types.
                        converted_parts = []
                        for content_block in tool_result_content:
                            if not isinstance(content_block, dict):
                                continue
                            content_type = content_block.get('type', 'text')

                            if content_type == 'text':
                                converted_parts.append(
                                    _copy_cache_control(
                                        content_block,
                                        {
                                            'type': 'text',
                                            'text': content_block.get('text', ''),
                                        },
                                    )
                                )
                            elif content_type == 'image':
                                source = content_block.get('source', {})
                                if source.get('type') == 'base64':
                                    media_type = source.get('media_type', 'image/png')
                                    data = source.get('data', '')
                                    converted_parts.append(
                                        _copy_cache_control(
                                            content_block,
                                            {
                                                'type': 'image_url',
                                                'image_url': {
                                                    'url': f'data:{media_type};base64,{data}',
                                                },
                                            },
                                        )
                                    )
                                elif source.get('type') == 'url':
                                    converted_parts.append(
                                        _copy_cache_control(
                                            content_block,
                                            {
                                                'type': 'image_url',
                                                'image_url': {
                                                    'url': source.get('url', ''),
                                                },
                                            },
                                        )
                                    )
                            elif content_type == 'document':
                                # Documents have no direct OpenAI equivalent;
                                # convert to a text representation.
                                document_source = content_block.get('source', {})
                                document_title = content_block.get('title', 'Document')
                                document_context = content_block.get('context', '')
                                document_text = f'[Document: {document_title}]'
                                if document_context:
                                    document_text += f'\n{document_context}'
                                if document_source.get('type') == 'text' and document_source.get('data'):
                                    document_text += f'\n{document_source["data"]}'
                                converted_parts.append({'type': 'text', 'text': document_text})
                            elif content_type == 'search_result':
                                # Convert search results to a text
                                # representation with source attribution.
                                search_title = content_block.get('title', '')
                                search_url = content_block.get('source', '')
                                search_content_blocks = content_block.get('content', [])
                                search_texts = []
                                for search_block in search_content_blocks:
                                    if isinstance(search_block, dict) and search_block.get('type') == 'text':
                                        search_texts.append(search_block.get('text', ''))
                                search_body = '\n'.join(search_texts)
                                search_text = f'[Search Result: {search_title}]'
                                if search_url:
                                    search_text += f'\nSource: {search_url}'
                                if search_body:
                                    search_text += f'\n{search_body}'
                                converted_parts.append({'type': 'text', 'text': search_text})

                        # Flatten to string when only text parts are present
                        if all(part.get('type') == 'text' for part in converted_parts) and not _has_cache_control(
                            converted_parts
                        ):
                            tool_content = '\n'.join(part.get('text', '') for part in converted_parts)
                        elif converted_parts:
                            tool_content = converted_parts
                        else:
                            tool_content = ''

                    # Propagate error status if present
                    if block.get('is_error'):
                        if isinstance(tool_content, str):
                            tool_content = f'Error: {tool_content}'
                        elif isinstance(tool_content, list):
                            tool_content.insert(
                                0,
                                {
                                    'type': 'text',
                                    'text': 'Error: ',
                                },
                            )

                    messages.append(
                        {
                            'role': 'tool',
                            'tool_call_id': block.get('tool_use_id', ''),
                            'content': tool_content,
                        }
                    )

            # Build the message
            if tool_calls:
                # Assistant message with tool calls
                msg_dict = {'role': role}
                if openai_content:
                    msg_dict['content'] = _finalize_openai_content(openai_content)
                else:
                    msg_dict['content'] = ''
                msg_dict['tool_calls'] = tool_calls
                messages.append(msg_dict)
            elif openai_content:
                messages.append({'role': role, 'content': _finalize_openai_content(openai_content)})
        else:
            messages.append({'role': role, 'content': str(content) if content else ''})

    openai_payload['messages'] = messages

    # max_tokens
    if 'max_tokens' in anthropic_payload:
        openai_payload['max_tokens'] = anthropic_payload['max_tokens']

    # Common parameters
    for param in ('temperature', 'top_p', 'top_k', 'stop_sequences', 'stream', 'metadata', 'service_tier'):
        if param in anthropic_payload:
            if param == 'stop_sequences':
                openai_payload['stop'] = anthropic_payload[param]
            else:
                openai_payload[param] = anthropic_payload[param]

    # Tools conversion: Anthropic → OpenAI
    if 'tools' in anthropic_payload:
        openai_tools = []
        for tool in anthropic_payload['tools']:
            openai_tools.append(
                _copy_cache_control(
                    tool,
                    {
                        'type': 'function',
                        'function': {
                            'name': tool.get('name', ''),
                            'description': tool.get('description', ''),
                            'parameters': tool.get('input_schema', {}),
                        },
                    },
                )
            )
        openai_payload['tools'] = openai_tools

    # tool_choice
    if 'tool_choice' in anthropic_payload:
        tool_choice = anthropic_payload['tool_choice']
        if isinstance(tool_choice, dict):
            tool_choice_type = tool_choice.get('type', 'auto')
            if tool_choice_type == 'auto':
                openai_payload['tool_choice'] = 'auto'
            elif tool_choice_type == 'any':
                openai_payload['tool_choice'] = 'required'
            elif tool_choice_type == 'tool':
                openai_payload['tool_choice'] = {
                    'type': 'function',
                    'function': {'name': tool_choice.get('name', '')},
                }

    return openai_payload


def convert_openai_to_anthropic_response(openai_response: dict, model: str = '') -> dict:
    """
    Convert a non-streaming OpenAI Chat Completions response to Anthropic Messages format.
    """
    import uuid as _uuid

    choice = {}
    if openai_response.get('choices'):
        choice = openai_response['choices'][0]

    message = choice.get('message', {})
    finish_reason = choice.get('finish_reason', 'stop')

    # Map finish_reason to stop_reason
    stop_reason_map = {
        'stop': 'end_turn',
        'length': 'max_tokens',
        'tool_calls': 'tool_use',
        'content_filter': 'end_turn',
    }
    stop_reason = stop_reason_map.get(finish_reason, 'end_turn')

    # Build content blocks
    content = []
    message_content = message.get('content')
    if message_content:
        content.append({'type': 'text', 'text': message_content})

    # Tool calls -> tool_use blocks
    tool_calls = message.get('tool_calls') or []
    for tool_call in tool_calls:
        function = tool_call.get('function', {})
        try:
            tool_input = json.loads(function.get('arguments', '{}'))
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
        content.append(
            {
                'type': 'tool_use',
                'id': tool_call.get('id', f'toolu_{_uuid.uuid4().hex[:24]}'),
                'name': function.get('name', ''),
                'input': tool_input,
            }
        )

    # Usage
    openai_usage = openai_response.get('usage', {})
    usage = {
        'input_tokens': openai_usage.get('anthropic_input_tokens', openai_usage.get('prompt_tokens', 0)),
        'output_tokens': openai_usage.get('anthropic_output_tokens', openai_usage.get('completion_tokens', 0)),
    }
    if 'cache_creation_input_tokens' in openai_usage:
        usage['cache_creation_input_tokens'] = openai_usage['cache_creation_input_tokens']
    if 'cache_read_input_tokens' in openai_usage:
        usage['cache_read_input_tokens'] = openai_usage['cache_read_input_tokens']

    return {
        'id': openai_response.get('id', f'msg_{_uuid.uuid4().hex[:24]}'),
        'type': 'message',
        'role': 'assistant',
        'content': content,
        'model': model or openai_response.get('model', ''),
        'stop_reason': stop_reason,
        'stop_sequence': None,
        'usage': usage,
    }


async def openai_stream_to_anthropic_stream(openai_stream_generator, model: str = ''):
    """
    Convert an OpenAI SSE streaming response to Anthropic Messages SSE format.

    OpenAI sends: data: {"choices": [{"delta": {"content": "..."}}]}
    Anthropic sends: event: content_block_delta\\ndata: {"type": "content_block_delta", ...}

    Handles text content, tool calls, and mixed content with proper
    multi-block indexing as required by Anthropic's streaming protocol.

    Tool calls are tracked by their unique id (not OpenAI index) so that
    parallel calls sharing the same index get distinct Anthropic tool_use
    blocks. Each block follows the Anthropic lifecycle: start -> delta -> stop.
    """
    import uuid as _uuid

    message_id = f'msg_{_uuid.uuid4().hex[:24]}'
    input_tokens = 0
    output_tokens = 0
    stop_reason = 'end_turn'

    # Track content blocks with a running index.
    # Each text block or tool_use block gets its own index.
    current_block_index = 0
    text_block_open = False

    # Accumulated state for each tool call, keyed by tool call id.
    # Parallel calls that share the same OpenAI index get distinct entries.
    # Each entry: {id, name, arguments, block_index, started, stopped}
    tracked_tool_calls = {}
    # Map OpenAI tool call index -> tool call id for routing
    # argument-only deltas (deltas that carry arguments but no id).
    index_to_tool_id = {}
    # Whether any tool call block has been emitted (suppresses further text)
    has_tool_calls = False

    # Emit message_start
    message_start = {
        'type': 'message_start',
        'message': {
            'id': message_id,
            'type': 'message',
            'role': 'assistant',
            'content': [],
            'model': model,
            'stop_reason': None,
            'stop_sequence': None,
            'usage': {'input_tokens': 0, 'output_tokens': 0},
        },
    }
    yield f'event: message_start\ndata: {json.dumps(message_start)}\n\n'.encode()

    try:
        async for chunk in openai_stream_generator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', errors='ignore')

            for line in chunk.strip().split('\n'):
                line = line.strip()

                if not line or not line.startswith('data:'):
                    continue

                data_string = line[5:].strip()
                if data_string == '[DONE]':
                    continue
                if data_string == '{}':
                    continue

                try:
                    data = json.loads(data_string)
                except (json.JSONDecodeError, TypeError):
                    continue

                choices = data.get('choices', [])
                if not choices:
                    # Check for usage in the final chunk
                    if data.get('usage'):
                        input_tokens = data['usage'].get('prompt_tokens', input_tokens)
                        output_tokens = data['usage'].get('completion_tokens', output_tokens)
                    continue

                delta = choices[0].get('delta', {})
                finish_reason = choices[0].get('finish_reason')
                message = choices[0].get('message') or {}

                # Update usage if present
                if data.get('usage'):
                    input_tokens = data['usage'].get('prompt_tokens', input_tokens)
                    output_tokens = data['usage'].get('completion_tokens', output_tokens)

                # --- Handle text content ---
                # Anthropic expects text blocks before tool blocks, so skip
                # text deltas once any tool call has started.
                content = delta.get('content')
                if content and not has_tool_calls:
                    if not text_block_open:
                        block_start = {
                            'type': 'content_block_start',
                            'index': current_block_index,
                            'content_block': {'type': 'text', 'text': ''},
                        }
                        yield f'event: content_block_start\ndata: {json.dumps(block_start)}\n\n'.encode()
                        text_block_open = True

                    block_delta = {
                        'type': 'content_block_delta',
                        'index': current_block_index,
                        'delta': {'type': 'text_delta', 'text': content},
                    }
                    yield f'event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n'.encode()

                # --- Handle tool calls ---
                # Some providers put tool_calls on the final message object
                # instead of the delta; fall back to that when needed.
                tool_calls = delta.get('tool_calls') or []
                if not tool_calls and message.get('tool_calls'):
                    tool_calls = message['tool_calls']

                if tool_calls:
                    # Close text block if one is open (text comes before tools)
                    if text_block_open:
                        block_stop = {
                            'type': 'content_block_stop',
                            'index': current_block_index,
                        }
                        yield f'event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n'.encode()
                        text_block_open = False
                        current_block_index += 1

                    for tool_call in tool_calls:
                        tool_call_index = tool_call.get('index', 0)
                        tool_call_id = tool_call.get('id', '')
                        tool_call_name = (tool_call.get('function') or {}).get('name', '')
                        arguments_chunk = (tool_call.get('function') or {}).get('arguments', '')

                        # Resolve which tracked tool call this delta belongs to.
                        # A delta with an id starts or identifies a specific tool.
                        # A delta without an id carries arguments for the most
                        # recent tool at this OpenAI index.
                        if tool_call_id:
                            if tool_call_id not in tracked_tool_calls:
                                tracked_tool_calls[tool_call_id] = {
                                    'id': tool_call_id,
                                    'name': tool_call_name,
                                    'arguments': '',
                                    'block_index': -1,
                                    'started': False,
                                    'stopped': False,
                                }
                            index_to_tool_id[tool_call_index] = tool_call_id
                            tool = tracked_tool_calls[tool_call_id]
                        elif tool_call_index in index_to_tool_id:
                            tool = tracked_tool_calls[index_to_tool_id[tool_call_index]]
                        else:
                            # First delta for this index with no id; create a
                            # provisional entry with a generated fallback id.
                            fallback_id = f'toolu_{_uuid.uuid4().hex[:24]}'
                            tracked_tool_calls[fallback_id] = {
                                'id': fallback_id,
                                'name': tool_call_name,
                                'arguments': '',
                                'block_index': -1,
                                'started': False,
                                'stopped': False,
                            }
                            index_to_tool_id[tool_call_index] = fallback_id
                            tool = tracked_tool_calls[fallback_id]

                        # Update name if provided on a later delta
                        if tool_call_name and not tool['name']:
                            tool['name'] = tool_call_name

                        # Emit content_block_start once we have a name
                        if not tool['started'] and tool['name']:
                            tool['block_index'] = current_block_index
                            tool['started'] = True
                            has_tool_calls = True

                            block_start = {
                                'type': 'content_block_start',
                                'index': current_block_index,
                                'content_block': {
                                    'type': 'tool_use',
                                    'id': tool['id'],
                                    'name': tool['name'],
                                    'input': {},
                                },
                            }
                            yield f'event: content_block_start\ndata: {json.dumps(block_start)}\n\n'.encode()
                            current_block_index += 1

                        # Buffer arguments and emit as input_json_delta
                        if arguments_chunk:
                            tool['arguments'] += arguments_chunk

                            if tool['started'] and not tool['stopped']:
                                block_delta = {
                                    'type': 'content_block_delta',
                                    'index': tool['block_index'],
                                    'delta': {
                                        'type': 'input_json_delta',
                                        'partial_json': arguments_chunk,
                                    },
                                }
                                yield f'event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n'.encode()

                            # Close the block once arguments form complete JSON
                            if tool['started'] and not tool['stopped']:
                                try:
                                    json.loads(tool['arguments'])
                                    tool['stopped'] = True
                                    block_stop = {
                                        'type': 'content_block_stop',
                                        'index': tool['block_index'],
                                    }
                                    yield f'event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n'.encode()
                                except (json.JSONDecodeError, ValueError):
                                    pass

                # --- Handle finish reason ---
                if finish_reason is not None:
                    stop_reason_map = {
                        'stop': 'end_turn',
                        'length': 'max_tokens',
                        'tool_calls': 'tool_use',
                    }
                    stop_reason = stop_reason_map.get(finish_reason, 'end_turn')

    except Exception as e:
        log.error(f'Error in Anthropic stream conversion: {e}')

    # Flush any tools that buffered arguments but never emitted a block
    for tool in tracked_tool_calls.values():
        if not tool['started'] and tool['name']:
            tool['block_index'] = current_block_index
            tool['started'] = True

            block_start = {
                'type': 'content_block_start',
                'index': current_block_index,
                'content_block': {
                    'type': 'tool_use',
                    'id': tool['id'],
                    'name': tool['name'],
                    'input': {},
                },
            }
            yield f'event: content_block_start\ndata: {json.dumps(block_start)}\n\n'.encode()
            current_block_index += 1

            if tool['arguments']:
                block_delta = {
                    'type': 'content_block_delta',
                    'index': tool['block_index'],
                    'delta': {
                        'type': 'input_json_delta',
                        'partial_json': tool['arguments'],
                    },
                }
                yield f'event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n'.encode()

    # Close any open text block
    if text_block_open:
        block_stop = {'type': 'content_block_stop', 'index': current_block_index}
        yield f'event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n'.encode()

    # Close any tool call blocks that are still open
    for tool in tracked_tool_calls.values():
        if tool['started'] and not tool['stopped']:
            block_stop = {'type': 'content_block_stop', 'index': tool['block_index']}
            yield f'event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n'.encode()

    # Emit message_delta with stop reason
    message_delta = {
        'type': 'message_delta',
        'delta': {
            'stop_reason': stop_reason,
            'stop_sequence': None,
        },
        'usage': {'output_tokens': output_tokens},
    }
    yield f'event: message_delta\ndata: {json.dumps(message_delta)}\n\n'.encode()

    # Emit message_stop
    yield f'event: message_stop\ndata: {json.dumps({"type": "message_stop"})}\n\n'.encode()

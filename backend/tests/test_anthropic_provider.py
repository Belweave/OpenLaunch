import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openlaunch.routers.anthropic import (
    AnthropicConfigForm,
    request_anthropic_chat_completion,
    update_config,
)
from openlaunch.utils.anthropic import (
    anthropic_stream_to_openai_stream,
    convert_anthropic_error_to_openai,
    convert_anthropic_to_openai_response,
    convert_openai_to_anthropic_payload,
    get_anthropic_headers,
    normalize_anthropic_base_url,
)


class FakeResponse:
    status = 200
    closed = False
    headers = {'Content-Type': 'application/json', 'request-id': 'req_test'}

    async def json(self):
        return {
            'id': 'msg_test',
            'type': 'message',
            'role': 'assistant',
            'model': 'claude-test',
            'content': [{'type': 'text', 'text': 'Hello'}],
            'stop_reason': 'end_turn',
            'stop_sequence': None,
            'usage': {'input_tokens': 3, 'output_tokens': 2},
        }

    async def text(self):
        return ''

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.url = None
        self.body = None
        self.headers = None

    async def post(self, url, data, headers, **_kwargs):
        self.url = url
        self.body = json.loads(data)
        self.headers = headers
        return FakeResponse()


class AnthropicConversionTests(unittest.TestCase):
    def test_config_update_preserves_active_global_model_cache(self):
        state = SimpleNamespace(
            ANTHROPIC_MODELS={'old-claude': {}},
            BASE_MODELS=[{'id': 'old-claude'}],
            MODELS={'active-model': {'id': 'active-model'}},
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        user = SimpleNamespace(id='admin-1')
        form = AnthropicConfigForm(
            ENABLE_ANTHROPIC_API=True,
            ANTHROPIC_API_BASE_URLS=['https://api.anthropic.com/v1'],
            ANTHROPIC_API_KEYS=['secret'],
            ANTHROPIC_API_CONFIGS={'0': {'auth_type': 'api_key'}},
        )

        with (
            patch('openlaunch.routers.anthropic.Config.upsert', AsyncMock()),
            patch('openlaunch.routers.anthropic.publish_event', AsyncMock()),
        ):
            asyncio.run(update_config(request, form, user))

        self.assertEqual(state.ANTHROPIC_MODELS, {})
        self.assertEqual(state.BASE_MODELS, [])
        self.assertEqual(state.MODELS, {'active-model': {'id': 'active-model'}})

    def test_normalizes_only_the_official_root_url(self):
        self.assertEqual(normalize_anthropic_base_url(None), 'https://api.anthropic.com/v1')
        self.assertEqual(
            normalize_anthropic_base_url('https://api.anthropic.com/'),
            'https://api.anthropic.com/v1',
        )
        self.assertEqual(normalize_anthropic_base_url('https://claude.example/api/'), 'https://claude.example/api')

    def test_native_headers_support_api_key_bearer_and_custom_compatible_auth(self):
        self.assertEqual(get_anthropic_headers('secret')['x-api-key'], 'secret')
        bearer = get_anthropic_headers('token', {'auth_type': 'bearer'})
        self.assertEqual(bearer['Authorization'], 'Bearer token')
        custom = get_anthropic_headers(
            'secret',
            {'headers': {'anthropic-version': '2024-01-01', 'X-Compatible': 'yes'}},
        )
        self.assertEqual(custom['anthropic-version'], '2024-01-01')
        self.assertEqual(custom['X-Compatible'], 'yes')

    def test_converts_system_multimodal_tools_and_tool_results(self):
        result = convert_openai_to_anthropic_payload(
            {
                'model': 'claude-test',
                'max_completion_tokens': 512,
                'stream': True,
                'stop': ['done'],
                'messages': [
                    {'role': 'system', 'content': 'Be concise.'},
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image_url',
                                'image_url': {'url': 'data:image/png;base64,aGVsbG8='},
                            },
                            {'type': 'text', 'text': 'What is this?'},
                        ],
                    },
                    {
                        'role': 'assistant',
                        'content': '',
                        'tool_calls': [
                            {
                                'id': 'call_1',
                                'type': 'function',
                                'function': {'name': 'lookup', 'arguments': '{"id":7}'},
                            }
                        ],
                    },
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found'},
                ],
                'tools': [
                    {
                        'type': 'function',
                        'function': {
                            'name': 'lookup',
                            'description': 'Find a record',
                            'parameters': {'type': 'object', 'properties': {'id': {'type': 'integer'}}},
                        },
                    }
                ],
                'tool_choice': 'required',
            }
        )
        self.assertEqual(result['system'], [{'type': 'text', 'text': 'Be concise.'}])
        self.assertEqual(result['messages'][0]['content'][0]['source']['type'], 'base64')
        self.assertEqual(result['messages'][1]['content'][0]['type'], 'tool_use')
        self.assertEqual(result['messages'][1]['content'][0]['input'], {'id': 7})
        self.assertEqual(result['messages'][2]['content'][0]['type'], 'tool_result')
        self.assertEqual(result['tools'][0]['input_schema']['type'], 'object')
        self.assertEqual(result['tool_choice'], {'type': 'any'})
        self.assertEqual(result['stop_sequences'], ['done'])

    def test_groups_parallel_tool_results_into_one_user_turn(self):
        result = convert_openai_to_anthropic_payload(
            {
                'model': 'claude-test',
                'messages': [
                    {'role': 'user', 'content': 'Use both tools.'},
                    {
                        'role': 'assistant',
                        'content': '',
                        'tool_calls': [
                            {
                                'id': 'call_search',
                                'type': 'function',
                                'function': {'name': 'search_web', 'arguments': '{}'},
                            },
                            {
                                'id': 'call_code',
                                'type': 'function',
                                'function': {'name': 'execute_code', 'arguments': '{}'},
                            },
                        ],
                    },
                    {'role': 'tool', 'tool_call_id': 'call_search', 'content': 'search result'},
                    {'role': 'tool', 'tool_call_id': 'call_code', 'content': 'code result'},
                ],
            }
        )

        self.assertEqual([message['role'] for message in result['messages']], ['user', 'assistant', 'user'])
        tool_results = result['messages'][2]['content']
        self.assertEqual([block['tool_use_id'] for block in tool_results], ['call_search', 'call_code'])
        self.assertTrue(all(block['type'] == 'tool_result' for block in tool_results))

    def test_converts_response_tools_usage_and_typed_errors(self):
        result = convert_anthropic_to_openai_response(
            {
                'id': 'msg_1',
                'model': 'claude-test',
                'content': [
                    {'type': 'text', 'text': 'Checking.'},
                    {'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup', 'input': {'id': 7}},
                ],
                'stop_reason': 'tool_use',
                'usage': {
                    'input_tokens': 10,
                    'cache_creation_input_tokens': 4,
                    'cache_read_input_tokens': 6,
                    'output_tokens': 3,
                    'service_tier': 'standard',
                },
            },
            request_id='req_1',
        )
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(result['choices'][0]['message']['tool_calls'][0]['function']['arguments'], '{"id":7}')
        self.assertEqual(result['usage']['prompt_tokens'], 20)
        self.assertEqual(result['usage']['anthropic_input_tokens'], 10)
        self.assertEqual(result['usage']['total_tokens'], 23)
        self.assertEqual(result['usage']['cache_read_input_tokens'], 6)
        self.assertEqual(result['request_id'], 'req_1')

        error = convert_anthropic_error_to_openai(
            {
                'type': 'error',
                'error': {'type': 'rate_limit_error', 'message': 'Slow down'},
                'request_id': 'req_error',
            }
        )
        self.assertEqual(error['error']['type'], 'rate_limit_error')
        self.assertEqual(error['request_id'], 'req_error')

    def test_stream_conversion_handles_split_crlf_frames_text_tools_and_usage(self):
        frames = [
            (
                'message_start',
                {'type': 'message_start', 'message': {'model': 'claude-test', 'usage': {'input_tokens': 5}}},
            ),
            (
                'content_block_start',
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            ),
            (
                'content_block_delta',
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hi'}},
            ),
            (
                'content_block_start',
                {
                    'type': 'content_block_start',
                    'index': 1,
                    'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup', 'input': {}},
                },
            ),
            (
                'content_block_delta',
                {
                    'type': 'content_block_delta',
                    'index': 1,
                    'delta': {'type': 'input_json_delta', 'partial_json': '{"id":7}'},
                },
            ),
            (
                'message_delta',
                {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 4}},
            ),
            ('message_stop', {'type': 'message_stop'}),
        ]
        wire = ''.join(f'event: {name}\r\ndata: {json.dumps(data)}\r\n\r\n' for name, data in frames).encode()

        async def source():
            for index in range(0, len(wire), 17):
                yield wire[index : index + 17]

        async def collect():
            return [chunk async for chunk in anthropic_stream_to_openai_stream(source())]

        output = b''.join(asyncio.run(collect())).decode()
        self.assertIn('"content": "Hi"', output)
        self.assertIn('"name": "lookup"', output)
        self.assertIn('"arguments": "{\\"id\\":7}"', output)
        self.assertIn('"finish_reason": "tool_calls"', output)
        self.assertIn('"prompt_tokens": 5', output)
        self.assertTrue(output.endswith('data: [DONE]\n\n'))

    def test_provider_request_uses_messages_endpoint_and_returns_common_response(self):
        session = FakeSession()
        request = SimpleNamespace()
        user = SimpleNamespace(id='user-1', name='User', email='user@example.com', role='admin')
        payload = {'model': 'claude-test', 'messages': [{'role': 'user', 'content': 'Hello'}]}

        with patch('openlaunch.routers.anthropic.get_session', AsyncMock(return_value=session)):
            result = asyncio.run(
                request_anthropic_chat_completion(
                    request,
                    payload,
                    user,
                    'https://api.anthropic.com',
                    'secret',
                )
            )

        self.assertEqual(session.url, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(session.headers['x-api-key'], 'secret')
        self.assertEqual(session.body['max_tokens'], 4096)
        self.assertEqual(result['choices'][0]['message']['content'], 'Hello')
        self.assertEqual(result['usage']['total_tokens'], 5)
        self.assertEqual(result['request_id'], 'req_test')


if __name__ == '__main__':
    unittest.main()

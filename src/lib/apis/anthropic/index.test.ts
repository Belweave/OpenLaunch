import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	anthropicChatCompletion,
	convertAnthropicToOpenAI,
	convertOpenAIToAnthropic
} from './index';

afterEach(() => {
	vi.restoreAllMocks();
});

describe('Anthropic direct connection adapter', () => {
	it('converts OpenAI messages, tools, and generation options to the Messages API', () => {
		const result = convertOpenAIToAnthropic({
			model: 'claude-sonnet-test',
			messages: [
				{ role: 'system', content: 'Be concise.' },
				{ role: 'user', content: 'Weather?' },
				{
					role: 'assistant',
					content: null,
					tool_calls: [
						{
							id: 'toolu_1',
							function: { name: 'weather', arguments: '{"city":"Boston"}' }
						}
					]
				},
				{ role: 'tool', tool_call_id: 'toolu_1', content: 'Sunny' }
			],
			max_tokens: 512,
			stream: true,
			stop: ['END'],
			tools: [
				{
					type: 'function',
					function: {
						name: 'weather',
						description: 'Get weather',
						parameters: { type: 'object', properties: { city: { type: 'string' } } }
					}
				}
			],
			tool_choice: 'required'
		}) as any;

		expect(result.system).toBe('Be concise.');
		expect(result.max_tokens).toBe(512);
		expect(result.stop_sequences).toEqual(['END']);
		expect(result.tool_choice).toEqual({ type: 'any' });
		expect(result.tools[0]).toMatchObject({
			name: 'weather',
			input_schema: { type: 'object' }
		});
		expect(result.messages[1].content[0]).toEqual({
			type: 'tool_use',
			id: 'toolu_1',
			name: 'weather',
			input: { city: 'Boston' }
		});
		expect(result.messages[2].content[0]).toEqual({
			type: 'tool_result',
			tool_use_id: 'toolu_1',
			content: 'Sunny'
		});
	});

	it('converts a non-streaming Anthropic response to an OpenAI completion', () => {
		const result = convertAnthropicToOpenAI({
			id: 'msg_1',
			model: 'claude-sonnet-test',
			content: [
				{ type: 'text', text: 'Calling it.' },
				{ type: 'tool_use', id: 'toolu_1', name: 'weather', input: { city: 'Boston' } }
			],
			stop_reason: 'tool_use',
			usage: { input_tokens: 10, output_tokens: 5 }
		}) as any;

		expect(result.choices[0].message.content).toBe('Calling it.');
		expect(result.choices[0].message.tool_calls[0]).toEqual({
			id: 'toolu_1',
			type: 'function',
			function: { name: 'weather', arguments: '{"city":"Boston"}' }
		});
		expect(result.choices[0].finish_reason).toBe('tool_calls');
		expect(result.usage).toEqual({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
	});

	it('uses Anthropic browser headers and translates streaming SSE events', async () => {
		const anthropicStream = [
			'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-test"}}\n\n',
			'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
			'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
			'event: message_stop\ndata: {"type":"message_stop"}\n\n'
		].join('');
		let upstreamCancelled = false;
		const body = new ReadableStream<Uint8Array>({
			start(controller) {
				controller.enqueue(new TextEncoder().encode(anthropicStream));
			},
			cancel() {
				upstreamCancelled = true;
			}
		});

		const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
			new Response(body, {
				status: 200,
				headers: { 'Content-Type': 'text/event-stream' }
			})
		);

		const [response] = await anthropicChatCompletion(
			'sk-ant-test',
			{
				model: 'claude-sonnet-test',
				messages: [{ role: 'user', content: 'Hi' }],
				stream: true
			},
			'https://api.anthropic.com'
		);

		expect(fetchMock).toHaveBeenCalledWith(
			'https://api.anthropic.com/v1/messages',
			expect.objectContaining({
				headers: expect.objectContaining({
					'x-api-key': 'sk-ant-test',
					'anthropic-version': '2023-06-01',
					'anthropic-dangerous-direct-browser-access': 'true'
				})
			})
		);

		const output = await response?.text();
		expect(output).toContain('"role":"assistant"');
		expect(output).toContain('"content":"Hello"');
		expect(output).toContain('"finish_reason":"stop"');
		expect(output).toContain('data: [DONE]');
		expect(upstreamCancelled).toBe(true);
	});
});

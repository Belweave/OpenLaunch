const ANTHROPIC_VERSION = '2023-06-01';

type OpenAIMessage = {
	role: string;
	content?: string | unknown[] | null;
	tool_call_id?: string;
	tool_calls?: Array<{
		id: string;
		type?: string;
		function: { name: string; arguments?: string };
	}>;
};

const getAnthropicEndpoint = (url: string, resource: string) => {
	const baseUrl = url.replace(/\/+$/, '');
	return `${baseUrl}${baseUrl.endsWith('/v1') ? '' : '/v1'}/${resource}`;
};

const getHeaders = (key: string) => ({
	Accept: 'application/json',
	'Content-Type': 'application/json',
	'x-api-key': key,
	'anthropic-version': ANTHROPIC_VERSION,
	'anthropic-dangerous-direct-browser-access': 'true'
});

const getErrorMessage = (error: any) =>
	error?.error?.message ?? error?.detail ?? error?.message ?? 'Network Problem';

export const getAnthropicModelsDirect = async (url: string, key: string) => {
	const models = [];
	let afterId = '';
	let hasMore = true;

	while (hasMore) {
		const endpoint = new URL(getAnthropicEndpoint(url, 'models'));
		if (afterId) endpoint.searchParams.set('after_id', afterId);

		let response: Response;
		try {
			response = await fetch(endpoint.toString(), {
				method: 'GET',
				headers: getHeaders(key)
			});
		} catch (error) {
			throw `Anthropic: ${getErrorMessage(error)}`;
		}

		const data = await response.json().catch(() => null);
		if (!response.ok) {
			throw `Anthropic: ${getErrorMessage(data)}`;
		}

		models.push(...(data?.data ?? []));
		hasMore = data?.has_more === true && Boolean(data?.last_id);
		afterId = data?.last_id ?? '';
	}

	return { object: 'list', data: models };
};

export const verifyAnthropicConnection = async (url: string, key: string) => {
	if (!url) throw 'Anthropic: URL is required';
	if (!key) throw 'Anthropic: API key is required';
	return getAnthropicModelsDirect(url, key);
};

const contentToText = (content: OpenAIMessage['content']) => {
	if (typeof content === 'string') return content;
	if (!Array.isArray(content)) return '';

	return content
		.filter((part: any) => ['text', 'input_text', 'output_text'].includes(part?.type))
		.map((part: any) => part.text ?? '')
		.join('\n');
};

const convertContent = (content: OpenAIMessage['content']) => {
	if (typeof content === 'string' || content == null) return content ?? '';
	if (!Array.isArray(content)) return String(content);

	const blocks: any[] = [];
	for (const part of content as any[]) {
		if (['text', 'input_text', 'output_text'].includes(part?.type)) {
			blocks.push({ type: 'text', text: part.text ?? '' });
			continue;
		}

		if (part?.type === 'image_url') {
			const imageUrl = typeof part.image_url === 'string' ? part.image_url : part.image_url?.url;
			if (!imageUrl) continue;

			const dataUrl = imageUrl.match(/^data:([^;,]+);base64,(.+)$/);
			blocks.push({
				type: 'image',
				source: dataUrl
					? { type: 'base64', media_type: dataUrl[1], data: dataUrl[2] }
					: { type: 'url', url: imageUrl }
			});
		}
	}

	return blocks;
};

const convertMessages = (messages: OpenAIMessage[] = []) => {
	const system = messages
		.filter((message) => ['system', 'developer'].includes(message.role))
		.map((message) => contentToText(message.content))
		.filter(Boolean)
		.join('\n\n');

	const anthropicMessages = messages
		.filter((message) => !['system', 'developer'].includes(message.role))
		.map((message) => {
			if (message.role === 'tool') {
				return {
					role: 'user',
					content: [
						{
							type: 'tool_result',
							tool_use_id: message.tool_call_id,
							content: contentToText(message.content)
						}
					]
				};
			}

			const content = convertContent(message.content);
			const blocks: any[] = Array.isArray(content)
				? content
				: content
					? [{ type: 'text', text: content }]
					: [];

			for (const toolCall of message.tool_calls ?? []) {
				let input = {};
				try {
					input = JSON.parse(toolCall.function.arguments || '{}');
				} catch {
					input = { value: toolCall.function.arguments };
				}

				blocks.push({
					type: 'tool_use',
					id: toolCall.id,
					name: toolCall.function.name,
					input
				});
			}

			return {
				role: message.role === 'assistant' ? 'assistant' : 'user',
				content: blocks
			};
		});

	return { system, messages: anthropicMessages };
};

export const convertOpenAIToAnthropic = (body: any) => {
	const { system, messages } = convertMessages(body.messages);
	const stopSequences = body.stop
		? Array.isArray(body.stop)
			? body.stop
			: [body.stop]
		: undefined;

	const tools = body.tools?.map((tool: any) => ({
		name: tool.function?.name ?? tool.name,
		description: tool.function?.description ?? tool.description,
		input_schema: tool.function?.parameters ??
			tool.input_schema ?? { type: 'object', properties: {} }
	}));

	let toolChoice;
	if (body.tool_choice) {
		if (typeof body.tool_choice === 'string') {
			toolChoice = {
				type: body.tool_choice === 'required' ? 'any' : body.tool_choice
			};
		} else if (body.tool_choice.function?.name) {
			toolChoice = { type: 'tool', name: body.tool_choice.function.name };
		}
	}

	return {
		model: body.model,
		messages,
		max_tokens: body.max_tokens ?? body.max_completion_tokens ?? 4096,
		...(system && { system }),
		...(body.stream !== undefined && { stream: body.stream }),
		...(body.temperature !== undefined && { temperature: body.temperature }),
		...(body.top_p !== undefined && { top_p: body.top_p }),
		...(body.top_k !== undefined && { top_k: body.top_k }),
		...(stopSequences && { stop_sequences: stopSequences }),
		...(tools?.length && { tools }),
		...(toolChoice && { tool_choice: toolChoice })
	};
};

const finishReason = (reason: string | null) => {
	if (reason === 'max_tokens') return 'length';
	if (reason === 'tool_use') return 'tool_calls';
	if (reason === 'refusal') return 'content_filter';
	return reason ? 'stop' : null;
};

const usageToOpenAI = (usage: any = {}) => {
	const promptTokens =
		(usage.input_tokens ?? 0) +
		(usage.cache_creation_input_tokens ?? 0) +
		(usage.cache_read_input_tokens ?? 0);
	const completionTokens = usage.output_tokens ?? 0;
	return {
		prompt_tokens: promptTokens,
		completion_tokens: completionTokens,
		total_tokens: promptTokens + completionTokens
	};
};

export const convertAnthropicToOpenAI = (message: any) => {
	const text = (message.content ?? [])
		.filter((block: any) => block.type === 'text')
		.map((block: any) => block.text)
		.join('');
	const toolCalls = (message.content ?? [])
		.filter((block: any) => block.type === 'tool_use')
		.map((block: any) => ({
			id: block.id,
			type: 'function',
			function: { name: block.name, arguments: JSON.stringify(block.input ?? {}) }
		}));

	return {
		id: message.id,
		object: 'chat.completion',
		created: Math.floor(Date.now() / 1000),
		model: message.model,
		choices: [
			{
				index: 0,
				message: {
					role: 'assistant',
					content: text,
					...(toolCalls.length && { tool_calls: toolCalls })
				},
				finish_reason: finishReason(message.stop_reason)
			}
		],
		usage: usageToOpenAI(message.usage)
	};
};

const openAIStreamEvent = (data: any) => `data: ${JSON.stringify(data)}\n\n`;

const transformAnthropicStream = (body: ReadableStream<Uint8Array>) => {
	const reader = body.getReader();
	const decoder = new TextDecoder();
	const encoder = new TextEncoder();
	let buffer = '';
	let id = '';
	let model = '';
	let streamUsage: any = {};
	let toolCallIndex = 0;
	let terminated = false;
	const toolIndexes = new Map<number, number>();

	const convertEvent = (event: any) => {
		if (event.type === 'message_start') {
			id = event.message?.id ?? id;
			model = event.message?.model ?? model;
			streamUsage = event.message?.usage ?? streamUsage;
			return openAIStreamEvent({
				id,
				object: 'chat.completion.chunk',
				model,
				choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }]
			});
		}

		if (event.type === 'content_block_start' && event.content_block?.type === 'tool_use') {
			const index = toolCallIndex++;
			toolIndexes.set(event.index, index);
			return openAIStreamEvent({
				id,
				object: 'chat.completion.chunk',
				model,
				choices: [
					{
						index: 0,
						delta: {
							tool_calls: [
								{
									index,
									id: event.content_block.id,
									type: 'function',
									function: { name: event.content_block.name, arguments: '' }
								}
							]
						},
						finish_reason: null
					}
				]
			});
		}

		if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
			return openAIStreamEvent({
				id,
				object: 'chat.completion.chunk',
				model,
				choices: [{ index: 0, delta: { content: event.delta.text }, finish_reason: null }]
			});
		}

		if (event.type === 'content_block_delta' && event.delta?.type === 'input_json_delta') {
			return openAIStreamEvent({
				id,
				object: 'chat.completion.chunk',
				model,
				choices: [
					{
						index: 0,
						delta: {
							tool_calls: [
								{
									index: toolIndexes.get(event.index) ?? event.index,
									function: { arguments: event.delta.partial_json }
								}
							]
						},
						finish_reason: null
					}
				]
			});
		}

		if (event.type === 'message_delta') {
			streamUsage = { ...streamUsage, ...(event.usage ?? {}) };
			return openAIStreamEvent({
				id,
				object: 'chat.completion.chunk',
				model,
				choices: [{ index: 0, delta: {}, finish_reason: finishReason(event.delta?.stop_reason) }],
				...(event.usage && { usage: usageToOpenAI(streamUsage) })
			});
		}

		if (event.type === 'message_stop') return 'data: [DONE]\n\n';
		if (event.type === 'error') {
			return openAIStreamEvent({ error: event.error ?? { message: 'Anthropic streaming error' } });
		}
		return '';
	};

	const processBuffer = (
		controller: ReadableStreamDefaultController<Uint8Array>,
		flush = false
	) => {
		const events = buffer.split(/\r?\n\r?\n/);
		buffer = flush ? '' : (events.pop() ?? '');

		for (const rawEvent of events) {
			const data = rawEvent
				.split(/\r?\n/)
				.filter((line) => line.startsWith('data:'))
				.map((line) => line.slice(5).trimStart())
				.join('\n');
			if (!data) continue;

			try {
				const event = JSON.parse(data);
				const output = convertEvent(event);
				if (output) controller.enqueue(encoder.encode(output));

				if (event.type === 'message_stop') {
					terminated = true;
					controller.close();
					void reader.cancel().catch(() => {});
					return true;
				}
			} catch (error) {
				console.error('Unable to parse Anthropic stream event', error);
			}
		}

		return false;
	};

	return new ReadableStream<Uint8Array>({
		async pull(controller) {
			if (terminated) return;

			const { done, value } = await reader.read();
			if (done) {
				if (buffer.trim()) buffer += '\n\n';
				const stopped = processBuffer(controller, true);
				if (!stopped) controller.close();
				return;
			}

			buffer += decoder.decode(value, { stream: true });
			processBuffer(controller);
		},
		cancel(reason) {
			return reader.cancel(reason);
		}
	});
};

export const anthropicChatCompletion = async (
	key: string,
	body: object,
	url: string,
	controller: AbortController = new AbortController()
): Promise<[Response | null, AbortController]> => {
	const anthropicBody = convertOpenAIToAnthropic(body);
	let response: Response;

	try {
		response = await fetch(getAnthropicEndpoint(url, 'messages'), {
			signal: controller.signal,
			method: 'POST',
			headers: getHeaders(key),
			body: JSON.stringify(anthropicBody)
		});
	} catch (error) {
		throw error;
	}

	if (!response.ok || !response.body) return [response, controller];

	if ((anthropicBody as any).stream) {
		return [
			new Response(transformAnthropicStream(response.body), {
				status: response.status,
				headers: { 'Content-Type': 'text/event-stream' }
			}),
			controller
		];
	}

	const data = await response.json();
	return [
		new Response(JSON.stringify(convertAnthropicToOpenAI(data)), {
			status: response.status,
			headers: { 'Content-Type': 'application/json' }
		}),
		controller
	];
};

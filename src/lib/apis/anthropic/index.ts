import { ANTHROPIC_API_BASE_URL } from '$lib/constants';

const parseError = async (response: Response) => {
	try {
		const body = await response.json();
		return (
			body?.error?.message ?? body?.detail?.message ?? body?.detail ?? 'Server connection failed'
		);
	} catch {
		return 'Server connection failed';
	}
};

export type AnthropicConfig = {
	ENABLE_ANTHROPIC_API: boolean;
	ANTHROPIC_API_BASE_URLS: string[];
	ANTHROPIC_API_KEYS: string[];
	ANTHROPIC_API_CONFIGS: Record<string, Record<string, unknown>>;
};

export const getAnthropicConfig = async (token: string = ''): Promise<AnthropicConfig> => {
	const response = await fetch(`${ANTHROPIC_API_BASE_URL}/config`, {
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	});
	if (!response.ok) throw await parseError(response);
	return response.json();
};

export const updateAnthropicConfig = async (
	token: string = '',
	config: AnthropicConfig
): Promise<AnthropicConfig> => {
	const response = await fetch(`${ANTHROPIC_API_BASE_URL}/config/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		},
		body: JSON.stringify(config)
	});
	if (!response.ok) throw await parseError(response);
	return response.json();
};

export const getAnthropicModels = async (token: string, urlIdx?: number) => {
	const response = await fetch(
		`${ANTHROPIC_API_BASE_URL}/models${typeof urlIdx === 'number' ? `/${urlIdx}` : ''}`,
		{
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` })
			}
		}
	);
	if (!response.ok) throw await parseError(response);
	return response.json();
};

export const verifyAnthropicConnection = async (
	token: string = '',
	connection: { url?: string; key: string; config?: Record<string, unknown> }
) => {
	const response = await fetch(`${ANTHROPIC_API_BASE_URL}/verify`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		},
		body: JSON.stringify(connection)
	});
	if (!response.ok) throw `Anthropic: ${await parseError(response)}`;
	return response.json();
};

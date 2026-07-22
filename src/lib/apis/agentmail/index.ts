import { OPENLAUNCH_API_BASE_URL } from '$lib/constants';

const request = async (token: string, path: string, options: RequestInit = {}) => {
	const headers = new Headers(options.headers);
	headers.set('Authorization', `Bearer ${token}`);
	if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
		headers.set('Content-Type', 'application/json');
	}
	const response = await fetch(`${OPENLAUNCH_API_BASE_URL}/agentmail${path}`, {
		...options,
		headers
	});
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw error.detail ?? 'AgentMail request failed';
	}
	if (response.status === 204 || response.headers.get('content-length') === '0') return null;
	return response.json();
};

export const getAgentMailAdminConfig = (token: string) => request(token, '/admin/config');

export const updateAgentMailAdminConfig = (token: string, body: object) =>
	request(token, '/admin/config', { method: 'POST', body: JSON.stringify(body) });

export const testAgentMailAdminConfig = (token: string) =>
	request(token, '/admin/test', { method: 'POST' });

export const getMyAgentMailInbox = (token: string) => request(token, '/me/inbox');

export const provisionMyAgentMailInbox = (token: string, body: object = {}) =>
	request(token, '/me/inbox', { method: 'POST', body: JSON.stringify(body) });

export const agentMailClient = (
	token: string,
	path: string,
	options: RequestInit = {},
	query: URLSearchParams | null = null
) => request(token, `/me/client/${path}${query ? `?${query.toString()}` : ''}`, options);

export const downloadAgentMailFile = async (token: string, path: string) => {
	const response = await fetch(`${OPENLAUNCH_API_BASE_URL}/agentmail/me/client/${path}`, {
		headers: { Authorization: `Bearer ${token}` }
	});
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw error.detail ?? 'Failed to download attachment';
	}
	return {
		blob: await response.blob(),
		filename:
			response.headers.get('content-disposition')?.match(/filename="?([^";]+)"?/)?.[1] ??
			'attachment'
	};
};

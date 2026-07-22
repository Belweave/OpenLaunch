import { OPENLAUNCH_API_BASE_URL } from '$lib/constants';

const request = async (token: string, path: string, options: RequestInit = {}) => {
	const response = await fetch(`${OPENLAUNCH_API_BASE_URL}/control-plane${path}`, {
		...options,
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`,
			...(options.headers ?? {})
		}
	});
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw new Error(error.detail ?? 'Control-plane request failed');
	}
	return response.json();
};

export const listDataConnections = (token: string) => request(token, '/data-connections');
export const saveDataConnection = (token: string, connection: Record<string, unknown>) =>
	request(token, `/data-connections/${connection.id}`, { method: 'PUT', body: JSON.stringify(connection) });
export const testDataConnection = (token: string, id: string) =>
	request(token, `/data-connections/${id}/test`, { method: 'POST' });
export const disableDataConnection = (token: string, id: string) =>
	request(token, `/data-connections/${id}/disable`, { method: 'POST' });
export const deleteDataConnection = (token: string, id: string) =>
	request(token, `/data-connections/${id}`, { method: 'DELETE' });

export const listToolProfiles = (token: string) => request(token, '/tool-profiles');
export const saveToolProfile = (token: string, profile: Record<string, unknown>) =>
	request(token, `/tool-profiles/${profile.id}`, { method: 'PUT', body: JSON.stringify(profile) });
export const deleteToolProfile = (token: string, id: string) =>
	request(token, `/tool-profiles/${id}`, { method: 'DELETE' });

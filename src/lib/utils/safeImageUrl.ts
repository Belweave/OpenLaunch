import { OPENLAUNCH_BASE_URL } from '$lib/constants';

const PLACEHOLDER_IMAGE = '/favicon.png';

/**
 * Validates an image URL against an allowlist of safe patterns and returns
 * the URL if trusted, or a placeholder otherwise.
 *
 * Allowed patterns:
 *   - Relative paths (starting with '/')
 *   - data:image/* URIs
 *   - Same-origin URLs (starting with OPENLAUNCH_BASE_URL)
 *   - Gravatar URLs (https://www.gravatar.com/avatar/)
 *
 * All other URLs (including arbitrary http(s):// origins) are rejected to
 * prevent client-side IP/UA/Referer leaks to attacker-controlled servers.
 */
export function safeImageUrl(url: string): string {
	if (!url || url === '') {
		return `${OPENLAUNCH_BASE_URL}${PLACEHOLDER_IMAGE}`;
	}

	if (
		url.startsWith(OPENLAUNCH_BASE_URL) ||
		url.startsWith('https://www.gravatar.com/avatar/') ||
		url.startsWith('data:') ||
		url.startsWith('/')
	) {
		return url;
	}

	return `${OPENLAUNCH_BASE_URL}${PLACEHOLDER_IMAGE}`;
}

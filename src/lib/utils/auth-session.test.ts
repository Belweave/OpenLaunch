import { describe, expect, it } from 'vitest';

import { hasAuthenticatedSession } from './auth-session';

describe('hasAuthenticatedSession', () => {
	it('keeps explicit and unresolved signed-out states on the auth page', () => {
		expect(hasAuthenticatedSession(undefined)).toBe(false);
		expect(hasAuthenticatedSession(null)).toBe(false);
	});

	it('redirects only for a resolved session user', () => {
		expect(hasAuthenticatedSession({})).toBe(false);
		expect(hasAuthenticatedSession({ id: '' })).toBe(false);
		expect(hasAuthenticatedSession({ id: 'user-1' })).toBe(true);
	});
});

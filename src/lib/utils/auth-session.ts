export const hasAuthenticatedSession = (sessionUser: { id?: string | null } | null | undefined) =>
	Boolean(sessionUser?.id);

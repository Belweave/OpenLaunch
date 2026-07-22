<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import DOMPurify from 'dompurify';

	import {
		agentMailClient,
		downloadAgentMailFile,
		getMyAgentMailDomains,
		getMyAgentMailInbox,
		provisionMyAgentMailInbox
	} from '$lib/apis/agentmail';
	import Envelope from '$lib/components/icons/Envelope.svelte';
	import Refresh from '$lib/components/icons/Refresh.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	let status: any = null;
	let provisioning = false;
	let preferredUsername = '';
	let domains: any[] = [{ domain: 'agentmail.to', default: true }];
	let selectedDomain = 'agentmail.to';
	let threads: any[] = [];
	let drafts: any[] = [];
	let selected: any = null;
	let selectedDraft: any = null;
	let loadingList = false;
	let loadingDetail = false;
	let folder = 'inbox';
	let searchQuery = '';
	let nextPageToken = '';
	let showCompose = false;
	let sending = false;
	let replyMode: 'reply' | 'reply-all' | null = null;
	let replyText = '';
	let forwardMessage: any = null;
	let composeFiles: File[] = [];
	let compose = { to: '', cc: '', bcc: '', subject: '', text: '', html: '', send_at: '' };

	const folders = [
		{ id: 'inbox', label: 'Inbox' },
		{ id: 'unread', label: 'Unread' },
		{ id: 'sent', label: 'Sent' },
		{ id: 'drafts', label: 'Drafts' },
		{ id: 'all', label: 'All mail' },
		{ id: 'trash', label: 'Trash' }
	];

	const addresses = (value: string) =>
		value
			.split(',')
			.map((item) => item.trim())
			.filter(Boolean);

	const displayAddress = (value: any) => {
		if (Array.isArray(value)) return value.join(', ');
		return value || '';
	};

	const formatDate = (value: string) => {
		if (!value) return '';
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
	};

	const loadStatus = async () => {
		status = await getMyAgentMailInbox(localStorage.token).catch((error) => {
			toast.error(String(error));
			return { enabled: true, configured: false, inbox: null };
		});
		if (status?.inbox) {
			await loadList();
		} else if (status?.enabled && status?.configured) {
			const result = await getMyAgentMailDomains(localStorage.token).catch((error) => {
				toast.error(String(error));
				return { domains };
			});
			domains = result.domains?.length ? result.domains : domains;
			if (!domains.some((item) => item.domain === selectedDomain)) {
				selectedDomain = domains[0].domain;
			}
		}
	};

	const provision = async () => {
		provisioning = true;
		try {
			const result = await provisionMyAgentMailInbox(localStorage.token, {
				username: preferredUsername || null,
				domain: selectedDomain
			});
			status = { ...status, inbox: result.inbox };
			toast.success(`Email ready at ${result.inbox.email}`);
			await loadList();
		} catch (error) {
			toast.error(String(error));
		} finally {
			provisioning = false;
		}
	};

	const loadList = async (append = false) => {
		if (!status?.inbox) return;
		loadingList = true;
		selected = null;
		selectedDraft = null;
		try {
			if (folder === 'drafts') {
				const query = new URLSearchParams({ limit: '50' });
				const result = await agentMailClient(localStorage.token, 'drafts', {}, query);
				drafts = result.drafts ?? [];
				nextPageToken = result.next_page_token ?? '';
				return;
			}

			const query = new URLSearchParams({ limit: '50' });
			if (append && nextPageToken) query.set('page_token', nextPageToken);
			if (folder === 'inbox') query.append('labels', 'received');
			if (folder === 'unread') query.append('labels', 'unread');
			if (folder === 'sent') query.append('labels', 'sent');
			if (folder === 'trash') {
				query.append('labels', 'trash');
				query.set('include_trash', 'true');
			}
			const path = searchQuery.trim() ? 'threads/search' : 'threads';
			if (searchQuery.trim()) query.set('q', searchQuery.trim());
			const result = await agentMailClient(localStorage.token, path, {}, query);
			threads = append ? [...threads, ...(result.threads ?? [])] : (result.threads ?? []);
			nextPageToken = result.next_page_token ?? '';
		} catch (error) {
			toast.error(String(error));
		} finally {
			loadingList = false;
		}
	};

	const chooseFolder = async (id: string) => {
		folder = id;
		searchQuery = '';
		threads = [];
		drafts = [];
		await loadList();
	};

	const openThread = async (thread: any) => {
		loadingDetail = true;
		selectedDraft = null;
		try {
			selected = await agentMailClient(
				localStorage.token,
				`threads/${encodeURIComponent(thread.thread_id)}`
			);
			const unread = [...(selected.messages ?? [])]
				.reverse()
				.find((message) => (message.labels ?? []).includes('unread'));
			if (unread) {
				await agentMailClient(
					localStorage.token,
					`messages/${encodeURIComponent(unread.message_id)}`,
					{
						method: 'PATCH',
						body: JSON.stringify({ add_labels: ['read'], remove_labels: ['unread'] })
					}
				);
			}
		} catch (error) {
			toast.error(String(error));
		} finally {
			loadingDetail = false;
		}
	};

	const openDraft = async (draft: any) => {
		loadingDetail = true;
		selected = null;
		try {
			selectedDraft = await agentMailClient(
				localStorage.token,
				`drafts/${encodeURIComponent(draft.draft_id)}`
			);
		} catch (error) {
			toast.error(String(error));
		} finally {
			loadingDetail = false;
		}
	};

	const filesToAttachments = async () =>
		Promise.all(
			composeFiles.map(
				(file) =>
					new Promise((resolve, reject) => {
						const reader = new FileReader();
						reader.onerror = reject;
						reader.onload = () =>
							resolve({
								filename: file.name,
								content_type: file.type || 'application/octet-stream',
								content: String(reader.result).split(',')[1]
							});
						reader.readAsDataURL(file);
					})
			)
		);

	const resetCompose = () => {
		compose = { to: '', cc: '', bcc: '', subject: '', text: '', html: '', send_at: '' };
		composeFiles = [];
		forwardMessage = null;
		showCompose = false;
	};

	const startCompose = () => {
		forwardMessage = null;
		showCompose = true;
	};

	const startForward = () => {
		forwardMessage = selected?.messages?.[selected.messages.length - 1] ?? null;
		compose = {
			...compose,
			subject: selected?.subject?.startsWith('Fwd:')
				? selected.subject
				: `Fwd: ${selected?.subject ?? ''}`,
			text: ''
		};
		showCompose = true;
	};

	const submitCompose = async (asDraft = false) => {
		if (!compose.to.trim() || !compose.subject.trim()) {
			toast.error('Recipients and subject are required');
			return;
		}
		sending = true;
		try {
			const body: any = {
				to: addresses(compose.to),
				subject: compose.subject,
				text: compose.text
			};
			if (compose.cc.trim()) body.cc = addresses(compose.cc);
			if (compose.bcc.trim()) body.bcc = addresses(compose.bcc);
			if (compose.html.trim()) body.html = compose.html;
			if (compose.send_at) body.send_at = new Date(compose.send_at).toISOString();
			if (composeFiles.length) body.attachments = await filesToAttachments();
			if (asDraft && forwardMessage) body.forward_of = forwardMessage.message_id;

			const path =
				!asDraft && !compose.send_at && forwardMessage
					? `messages/${encodeURIComponent(forwardMessage.message_id)}/forward`
					: asDraft || compose.send_at
						? 'drafts'
						: 'messages/send';
			await agentMailClient(localStorage.token, path, {
				method: 'POST',
				body: JSON.stringify(body)
			});
			toast.success(asDraft || compose.send_at ? 'Draft saved' : 'Email sent');
			resetCompose();
			await loadList();
		} catch (error) {
			toast.error(String(error));
		} finally {
			sending = false;
		}
	};

	const sendReply = async () => {
		const message = selected?.messages?.[selected.messages.length - 1];
		if (!message || !replyText.trim() || !replyMode) return;
		sending = true;
		try {
			await agentMailClient(
				localStorage.token,
				`messages/${encodeURIComponent(message.message_id)}/${replyMode}`,
				{ method: 'POST', body: JSON.stringify({ text: replyText }) }
			);
			replyText = '';
			replyMode = null;
			await openThread(selected);
			toast.success('Reply sent');
		} catch (error) {
			toast.error(String(error));
		} finally {
			sending = false;
		}
	};

	const trashThread = async () => {
		for (const message of selected?.messages ?? []) {
			await agentMailClient(
				localStorage.token,
				`messages/${encodeURIComponent(message.message_id)}`,
				{
					method: 'PATCH',
					body: JSON.stringify({ add_labels: ['trash'] })
				}
			);
		}
		selected = null;
		await loadList();
	};

	const sendDraft = async () => {
		if (!selectedDraft) return;
		await agentMailClient(
			localStorage.token,
			`drafts/${encodeURIComponent(selectedDraft.draft_id)}/send`,
			{ method: 'POST' }
		);
		toast.success('Draft sent');
		selectedDraft = null;
		await loadList();
	};

	const downloadAttachment = async (message: any, attachment: any) => {
		try {
			const file = await downloadAgentMailFile(
				localStorage.token,
				`messages/${encodeURIComponent(message.message_id)}/attachments/${encodeURIComponent(attachment.attachment_id)}`
			);
			const url = URL.createObjectURL(file.blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = attachment.filename || file.filename;
			anchor.click();
			URL.revokeObjectURL(url);
		} catch (error) {
			toast.error(String(error));
		}
	};

	onMount(loadStatus);
</script>

<svelte:head><title>Email</title></svelte:head>

<div
	class="flex h-screen min-h-0 w-full bg-white text-gray-900 dark:bg-gray-900 dark:text-gray-100"
>
	{#if status === null}
		<div class="flex w-full items-center justify-center text-sm text-gray-500">Loading email…</div>
	{:else if !status.enabled}
		<div
			class="m-auto max-w-lg rounded-2xl border border-gray-200 p-8 text-center dark:border-gray-800"
		>
			<Envelope className="mx-auto mb-4 size-10" />
			<h1 class="text-xl font-semibold">Email is not enabled</h1>
			<p class="mt-2 text-sm text-gray-500">
				An administrator can enable AgentMail in Admin Settings → General.
			</p>
		</div>
	{:else if !status.inbox}
		<div class="m-auto w-full max-w-lg rounded-2xl border border-gray-200 p-8 dark:border-gray-800">
			<Envelope className="mb-4 size-10" />
			<h1 class="text-xl font-semibold">Set up your AgentMail inbox</h1>
			<p class="mt-2 text-sm text-gray-500">
				OpenLaunch checked for an existing linked inbox. Choose an address name and one of your
				AgentMail domains, or leave the name empty to generate one.
			</p>
			<div
				class="mt-5 flex items-center overflow-hidden rounded-xl border border-gray-200 dark:border-gray-700"
			>
				<input
					class="min-w-0 flex-1 bg-transparent px-4 py-2.5 text-sm outline-none"
					placeholder="preferred-name"
					bind:value={preferredUsername}
				/>
				<span class="text-sm text-gray-400">@</span>
				<select
					class="max-w-[50%] bg-transparent py-2.5 pr-4 text-sm outline-none"
					bind:value={selectedDomain}
					aria-label="Email domain"
				>
					{#each domains as item}
						<option value={item.domain}>{item.domain}</option>
					{/each}
				</select>
			</div>
			<button
				class="mt-4 w-full rounded-xl bg-black px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-black"
				disabled={provisioning}
				on:click={provision}
			>
				{provisioning ? 'Creating inbox…' : 'Provision inbox'}
			</button>
		</div>
	{:else}
		<aside class="hidden w-48 shrink-0 border-r border-gray-100 p-3 dark:border-gray-800 md:block">
			<button
				class="mb-4 w-full rounded-xl bg-black px-4 py-2.5 text-sm font-medium text-white dark:bg-white dark:text-black"
				on:click={startCompose}>Compose</button
			>
			<div class="mb-4 truncate px-2 text-xs text-gray-500" title={status.inbox.email}>
				{status.inbox.email}
			</div>
			{#each folders as item}
				<button
					class="mb-0.5 w-full rounded-lg px-3 py-2 text-left text-sm {folder === item.id
						? 'bg-gray-100 font-medium dark:bg-gray-800'
						: 'hover:bg-gray-50 dark:hover:bg-gray-850'}"
					on:click={() => chooseFolder(item.id)}>{item.label}</button
				>
			{/each}
		</aside>

		<section
			class="flex w-full max-w-sm shrink-0 flex-col border-r border-gray-100 dark:border-gray-800"
		>
			<div class="border-b border-gray-100 p-3 dark:border-gray-800">
				<div class="flex items-center justify-between gap-2">
					<h1 class="text-lg font-semibold capitalize">{folder === 'all' ? 'All mail' : folder}</h1>
					<div class="flex gap-1">
						<button
							class="rounded-lg p-2 hover:bg-gray-100 dark:hover:bg-gray-800 md:hidden"
							on:click={startCompose}
							title="Compose"><Envelope className="size-4" /></button
						>
						<button
							class="rounded-lg p-2 hover:bg-gray-100 dark:hover:bg-gray-800"
							on:click={() => loadList()}
							title="Refresh"><Refresh className="size-4" /></button
						>
					</div>
				</div>
				<div class="mt-3 flex items-center rounded-lg bg-gray-100 px-3 dark:bg-gray-800">
					<Search className="size-4 text-gray-400" />
					<input
						class="w-full bg-transparent px-2 py-2 text-sm outline-none"
						placeholder="Search email"
						bind:value={searchQuery}
						on:keydown={(event) => event.key === 'Enter' && loadList()}
					/>
				</div>
				<div class="mt-2 flex gap-1 overflow-x-auto md:hidden">
					{#each folders as item}<button
							class="whitespace-nowrap rounded-full px-2.5 py-1 text-xs {folder === item.id
								? 'bg-gray-900 text-white dark:bg-white dark:text-black'
								: 'bg-gray-100 dark:bg-gray-800'}"
							on:click={() => chooseFolder(item.id)}>{item.label}</button
						>{/each}
				</div>
			</div>

			<div class="min-h-0 flex-1 overflow-y-auto">
				{#if loadingList}<div class="p-6 text-center text-sm text-gray-500">Loading…</div>{/if}
				{#if folder === 'drafts'}
					{#each drafts as draft}
						<button
							class="w-full border-b border-gray-100 p-4 text-left hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-850"
							on:click={() => openDraft(draft)}
						>
							<div class="flex justify-between gap-3">
								<span class="truncate text-sm font-medium">To: {displayAddress(draft.to)}</span
								><span class="shrink-0 text-xs text-gray-400">{formatDate(draft.updated_at)}</span>
							</div>
							<div class="mt-1 truncate text-sm">{draft.subject || '(no subject)'}</div>
							<div class="mt-1 truncate text-xs text-gray-500">{draft.preview || draft.text}</div>
						</button>
					{/each}
				{:else}
					{#each threads as thread}
						<button
							class="w-full border-b border-gray-100 p-4 text-left hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-850"
							on:click={() => openThread(thread)}
						>
							<div class="flex justify-between gap-3">
								<span
									class="truncate text-sm {(thread.labels ?? []).includes('unread')
										? 'font-bold'
										: 'font-medium'}"
									>{displayAddress(thread.senders) || displayAddress(thread.recipients)}</span
								><span class="shrink-0 text-xs text-gray-400">{formatDate(thread.timestamp)}</span>
							</div>
							<div class="mt-1 truncate text-sm">{thread.subject || '(no subject)'}</div>
							<div class="mt-1 truncate text-xs text-gray-500">{thread.preview}</div>
						</button>
					{/each}
				{/if}
				{#if !loadingList && ((folder === 'drafts' && drafts.length === 0) || (folder !== 'drafts' && threads.length === 0))}<div
						class="p-8 text-center text-sm text-gray-500"
					>
						Nothing here yet.
					</div>{/if}
				{#if nextPageToken}<button
						class="w-full p-3 text-sm text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-850"
						on:click={() => loadList(true)}>Load more</button
					>{/if}
			</div>
		</section>

		<main
			class="{selected || selectedDraft
				? 'fixed inset-0 z-40 block md:static'
				: 'hidden md:block'} min-w-0 flex-1 overflow-y-auto bg-white dark:bg-gray-900"
		>
			{#if loadingDetail}<div class="p-8 text-sm text-gray-500">Loading conversation…</div>
			{:else if selected}
				<div
					class="sticky top-0 z-10 flex items-center justify-between border-b border-gray-100 bg-white/95 p-4 backdrop-blur dark:border-gray-800 dark:bg-gray-900/95"
				>
					<div class="flex min-w-0 items-center gap-2">
						<button
							class="rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 md:hidden"
							on:click={() => (selected = null)}><XMark className="size-5" /></button
						>
						<div class="min-w-0">
							<h2 class="truncate text-lg font-semibold">{selected.subject || '(no subject)'}</h2>
							<div class="text-xs text-gray-500">
								{selected.message_count} message{selected.message_count === 1 ? '' : 's'}
							</div>
						</div>
					</div>
					<div class="flex gap-2">
						<button
							class="rounded-lg px-3 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-800"
							on:click={() => (replyMode = 'reply')}>Reply</button
						><button
							class="hidden rounded-lg px-3 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-800 sm:block"
							on:click={() => (replyMode = 'reply-all')}>Reply all</button
						><button
							class="hidden rounded-lg px-3 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-800 sm:block"
							on:click={startForward}>Forward</button
						><button
							class="rounded-lg px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 dark:hover:bg-red-950/30"
							on:click={trashThread}>Trash</button
						>
					</div>
				</div>
				<div class="mx-auto max-w-4xl p-5">
					{#each selected.messages ?? [] as message}
						<article class="mb-4 rounded-xl border border-gray-100 p-5 dark:border-gray-800">
							<div class="flex justify-between gap-4">
								<div class="min-w-0">
									<div class="truncate text-sm font-medium">{message.from}</div>
									<div class="truncate text-xs text-gray-500">to {displayAddress(message.to)}</div>
								</div>
								<time class="shrink-0 text-xs text-gray-400">{formatDate(message.timestamp)}</time>
							</div>
							<div class="prose prose-sm mt-5 max-w-none dark:prose-invert">
								{#if message.html}{@html DOMPurify.sanitize(message.html)}{:else}<div
										class="whitespace-pre-wrap"
									>
										{message.text || message.extracted_text || message.preview}
									</div>{/if}
							</div>
							{#if message.attachments?.length}<div class="mt-4 flex flex-wrap gap-2">
									{#each message.attachments as attachment}<button
											class="rounded-lg border border-gray-200 px-3 py-2 text-xs hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800"
											on:click={() => downloadAttachment(message, attachment)}
											>📎 {attachment.filename || 'Attachment'} ({Math.ceil(
												(attachment.size || 0) / 1024
											)} KB)</button
										>{/each}
								</div>{/if}
						</article>
					{/each}
					{#if replyMode}<div class="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
							<div class="mb-2 text-sm font-medium">
								{replyMode === 'reply-all' ? 'Reply all' : 'Reply'}
							</div>
							<textarea
								class="min-h-32 w-full resize-y bg-transparent text-sm outline-none"
								placeholder="Write a reply…"
								bind:value={replyText}
							></textarea>
							<div class="mt-3 flex justify-end gap-2">
								<button class="rounded-lg px-3 py-2 text-sm" on:click={() => (replyMode = null)}
									>Cancel</button
								><button
									class="rounded-lg bg-black px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
									disabled={sending || !replyText.trim()}
									on:click={sendReply}>{sending ? 'Sending…' : 'Send reply'}</button
								>
							</div>
						</div>{/if}
				</div>
			{:else if selectedDraft}
				<div class="mx-auto max-w-3xl p-8">
					<div class="flex items-center justify-between gap-3">
						<div class="flex min-w-0 items-center gap-2">
							<button
								class="rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 md:hidden"
								on:click={() => (selectedDraft = null)}><XMark className="size-5" /></button
							>
							<h2 class="truncate text-xl font-semibold">
								{selectedDraft.subject || '(no subject)'}
							</h2>
						</div>
						<button
							class="rounded-lg bg-black px-4 py-2 text-sm text-white dark:bg-white dark:text-black"
							on:click={sendDraft}>Send draft</button
						>
					</div>
					<div class="mt-2 text-sm text-gray-500">To: {displayAddress(selectedDraft.to)}</div>
					<div class="mt-6 whitespace-pre-wrap text-sm">
						{selectedDraft.text || selectedDraft.preview}
					</div>
				</div>
			{:else}<div class="flex h-full items-center justify-center text-sm text-gray-400">
					Select a conversation
				</div>{/if}
		</main>
	{/if}
</div>

{#if showCompose}
	<div
		class="fixed inset-0 z-[100] flex items-end justify-center bg-black/30 p-0 sm:items-center sm:p-6"
	>
		<div
			class="flex max-h-[92vh] w-full max-w-2xl flex-col rounded-t-2xl bg-white shadow-2xl dark:bg-gray-850 sm:rounded-2xl"
		>
			<div
				class="flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800"
			>
				<h2 class="font-semibold">New message</h2>
				<button
					class="rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-800"
					on:click={resetCompose}><XMark className="size-5" /></button
				>
			</div>
			<div class="min-h-0 space-y-3 overflow-y-auto p-5">
				<input
					class="w-full rounded-lg bg-gray-50 px-4 py-2.5 text-sm outline-none dark:bg-gray-800"
					placeholder="To (comma separated)"
					bind:value={compose.to}
				/>
				<div class="grid grid-cols-2 gap-3">
					<input
						class="rounded-lg bg-gray-50 px-4 py-2.5 text-sm outline-none dark:bg-gray-800"
						placeholder="Cc"
						bind:value={compose.cc}
					/><input
						class="rounded-lg bg-gray-50 px-4 py-2.5 text-sm outline-none dark:bg-gray-800"
						placeholder="Bcc"
						bind:value={compose.bcc}
					/>
				</div>
				<input
					class="w-full rounded-lg bg-gray-50 px-4 py-2.5 text-sm outline-none dark:bg-gray-800"
					placeholder="Subject"
					bind:value={compose.subject}
				/>
				<textarea
					class="min-h-56 w-full resize-y rounded-lg bg-gray-50 px-4 py-3 text-sm outline-none dark:bg-gray-800"
					placeholder="Write your email…"
					bind:value={compose.text}
				></textarea>
				<details>
					<summary class="cursor-pointer text-xs text-gray-500">HTML and scheduling options</summary
					><textarea
						class="mt-2 min-h-28 w-full rounded-lg bg-gray-50 px-4 py-3 font-mono text-xs outline-none dark:bg-gray-800"
						placeholder="Optional HTML body"
						bind:value={compose.html}
					></textarea><label class="mt-2 block text-xs text-gray-500"
						>Schedule send <input
							class="ml-2 rounded-lg bg-gray-50 px-3 py-2 dark:bg-gray-800"
							type="datetime-local"
							bind:value={compose.send_at}
						/></label
					>
				</details>
				<label
					class="block cursor-pointer rounded-lg border border-dashed border-gray-300 p-3 text-center text-xs text-gray-500 dark:border-gray-700"
					>Add attachments<input
						class="hidden"
						type="file"
						multiple
						on:change={(event) =>
							(composeFiles = [...composeFiles, ...Array.from(event.currentTarget.files ?? [])])}
					/></label
				>
				{#if composeFiles.length}<div class="flex flex-wrap gap-2">
						{#each composeFiles as file, index}<button
								class="rounded-full bg-gray-100 px-3 py-1 text-xs dark:bg-gray-800"
								on:click={() => (composeFiles = composeFiles.filter((_, i) => i !== index))}
								>{file.name} ×</button
							>{/each}
					</div>{/if}
			</div>
			<div class="flex justify-between border-t border-gray-100 p-4 dark:border-gray-800">
				<button
					class="rounded-lg px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
					disabled={sending}
					on:click={() => submitCompose(true)}>Save draft</button
				><button
					class="rounded-lg bg-black px-5 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-black"
					disabled={sending}
					on:click={() => submitCompose(false)}
					>{sending ? 'Working…' : compose.send_at ? 'Schedule' : 'Send'}</button
				>
			</div>
		</div>
	</div>
{/if}

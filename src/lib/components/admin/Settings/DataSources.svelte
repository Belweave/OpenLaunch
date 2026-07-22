<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		deleteDataConnection,
		deleteToolProfile,
		disableDataConnection,
		listDataConnections,
		listToolProfiles,
		saveDataConnection,
		saveToolProfile,
		testDataConnection
	} from '$lib/apis/control-plane';

	let connections: any[] = [];
	let profiles: any[] = [];
	let connection = emptyConnection();
	let profile = emptyProfile();

	function emptyConnection() {
		return {
			id: '', scope_type: 'instance', scope_id: '*', provider_type: 'postgresql', description: '',
			enabled: true, safe_metadata: '{}', secret_ref: '{"type":"env","name":"DATA_SOURCE_URL","field":"url"}',
			policy: '{}', access_grants: '[]'
		};
	}
	function emptyProfile() {
		return { id: '', name: '', description: '', enabled: true, assignments: '[{"scope_type":"instance","scope_id":"*"}]', bundle: '{"empty":true}' };
	}
	const parse = (value: string) => JSON.parse(value || '{}');
	const refresh = async () => {
		[connections, profiles] = await Promise.all([
			listDataConnections(localStorage.token),
			listToolProfiles(localStorage.token)
		]);
	};
	const editConnection = (item: any) => connection = {
		...item,
		safe_metadata: JSON.stringify(item.safe_metadata ?? {}, null, 2),
		secret_ref: item.has_secret ? '' : '{"type":"env","name":"DATA_SOURCE_URL","field":"url"}',
		policy: JSON.stringify(item.policy ?? {}, null, 2),
		access_grants: JSON.stringify(item.access_grants ?? [], null, 2)
	};
	const editProfile = (item: any) => profile = {
		...item,
		assignments: JSON.stringify(item.assignments ?? [], null, 2),
		bundle: JSON.stringify(item.bundle ?? {}, null, 2)
	};
	const submitConnection = async () => {
		try {
			await saveDataConnection(localStorage.token, {
				...connection,
				safe_metadata: parse(connection.safe_metadata),
				...(connection.secret_ref ? { secret_ref: parse(connection.secret_ref) } : {}),
				policy: parse(connection.policy),
				access_grants: JSON.parse(connection.access_grants || '[]')
			});
			toast.success('Data connection saved'); connection = emptyConnection(); await refresh();
		} catch (error) { toast.error(`${error}`); }
	};
	const submitProfile = async () => {
		try {
			await saveToolProfile(localStorage.token, {
				...profile, assignments: JSON.parse(profile.assignments || '[]'), bundle: parse(profile.bundle)
			});
			toast.success('Tool profile saved'); profile = emptyProfile(); await refresh();
		} catch (error) { toast.error(`${error}`); }
	};
	onMount(refresh);
</script>

<div class="space-y-8 text-sm pb-8">
	<section class="space-y-3">
		<div><h2 class="font-semibold text-base">Data connections</h2><p class="text-xs text-gray-500">Persist safe metadata separately from rotating environment or file secret references.</p></div>
		<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
			<input class="input" placeholder="Connection ID" bind:value={connection.id} />
			<select class="input" bind:value={connection.provider_type}><option>postgresql</option><option>sql_server</option><option>azure_sql</option><option>snowflake</option><option>redis</option></select>
			<input class="input" placeholder="Description" bind:value={connection.description} />
			<input class="input" placeholder="Scope ID" bind:value={connection.scope_id} />
		</div>
		<label class="block text-xs">Safe metadata JSON<textarea class="input h-24" bind:value={connection.safe_metadata}></textarea></label>
		<label class="block text-xs">Secret reference JSON<textarea class="input h-20" bind:value={connection.secret_ref}></textarea></label>
		<label class="block text-xs">Policy JSON<textarea class="input h-28" bind:value={connection.policy}></textarea></label>
		<label class="block text-xs">Access grants JSON<textarea class="input h-20" bind:value={connection.access_grants}></textarea></label>
		<button class="primary" on:click={submitConnection}>Save connection</button>
		<div class="divide-y dark:divide-gray-800">
			{#each connections as item}
				<div class="py-2 flex items-center justify-between gap-3"><div><b>{item.id}</b> <span class="text-xs text-gray-500">{item.provider_type} · {item.enabled ? 'enabled' : 'disabled'}</span></div><div class="flex gap-2"><button on:click={() => editConnection(item)}>Edit</button><button on:click={async () => { try { await testDataConnection(localStorage.token, item.id); toast.success('Connection succeeded'); } catch (e) { toast.error(`${e}`); } }}>Test</button><button on:click={async () => { await disableDataConnection(localStorage.token, item.id); await refresh(); }}>Disable</button><button class="text-red-600" on:click={async () => { await deleteDataConnection(localStorage.token, item.id); await refresh(); }}>Delete</button></div></div>
			{/each}
		</div>
	</section>

	<section class="space-y-3">
		<div><h2 class="font-semibold text-base">API tool profiles</h2><p class="text-xs text-gray-500">Explicit bundles for API credentials, users, workspaces, organizations, models, and service accounts.</p></div>
		<div class="grid grid-cols-1 md:grid-cols-2 gap-2"><input class="input" placeholder="Profile ID" bind:value={profile.id} /><input class="input" placeholder="Name" bind:value={profile.name} /></div>
		<label class="block text-xs">Assignments JSON<textarea class="input h-24" bind:value={profile.assignments}></textarea></label>
		<label class="block text-xs">Bundle JSON<textarea class="input h-24" bind:value={profile.bundle}></textarea></label>
		<button class="primary" on:click={submitProfile}>Save profile</button>
		{#each profiles as item}<div class="py-2 flex justify-between"><span><b>{item.name}</b> <span class="text-xs text-gray-500">{item.id}</span></span><span class="flex gap-2"><button on:click={() => editProfile(item)}>Edit</button><button class="text-red-600" on:click={async () => { await deleteToolProfile(localStorage.token, item.id); await refresh(); }}>Delete</button></span></div>{/each}
	</section>
</div>

<style>
	:global(.input) { width: 100%; border-radius: 0.5rem; padding: 0.5rem 0.65rem; background: rgba(127,127,127,.08); outline: none; }
	:global(.primary) { border-radius: 0.5rem; padding: 0.5rem 0.8rem; background: #2563eb; color: white; }
</style>

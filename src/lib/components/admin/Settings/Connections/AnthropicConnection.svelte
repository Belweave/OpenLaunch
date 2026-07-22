<script lang="ts">
	import { getContext } from 'svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import AddConnectionModal from '$lib/components/AddConnectionModal.svelte';

	const i18n = getContext('i18n');
	export let onDelete = () => {};
	export let onSubmit = (_connection) => {};
	export let url = '';
	export let key = '';
	export let config = {};
	let showConfigModal = false;
</script>

<AddConnectionModal
	edit
	anthropic
	bind:show={showConfigModal}
	connection={{ url, key, config }}
	onDelete={() => {
		onDelete();
		showConfigModal = false;
	}}
	onSubmit={(connection) => {
		url = connection.url;
		key = connection.key;
		config = connection.config;
		onSubmit(connection);
	}}
/>

<div class="flex w-full gap-2 items-center">
	<Tooltip
		className="w-full relative"
		content={$i18n.t(`OpenLaunch will make requests to "{{url}}/messages"`, { url })}
		placement="top-start"
	>
		{#if !(config?.enable ?? true)}
			<div class="absolute inset-0 opacity-60 bg-white dark:bg-gray-900 z-10"></div>
		{/if}
		<input
			class="outline-hidden w-full bg-transparent"
			placeholder={$i18n.t('Anthropic API Base URL')}
			bind:value={url}
			autocomplete="off"
			readonly
		/>
	</Tooltip>

	<div class="flex gap-1 items-center">
		<Tooltip content={$i18n.t('Configure')} className="self-start">
			<button
				class="self-center p-1 bg-transparent hover:bg-gray-100 dark:hover:bg-gray-850 rounded-lg transition"
				on:click={() => (showConfigModal = true)}
				type="button"
				aria-label={$i18n.t('Configure')}
			>
				<Cog6 />
			</button>
		</Tooltip>
		<Tooltip content={(config?.enable ?? true) ? $i18n.t('Enabled') : $i18n.t('Disabled')}>
			<Switch
				bind:state={config.enable}
				on:change={() => {
					config.enable = config.enable ?? false;
					onSubmit({ url, key, config });
				}}
			/>
		</Tooltip>
	</div>
</div>

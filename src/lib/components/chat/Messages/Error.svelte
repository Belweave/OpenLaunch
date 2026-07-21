<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import Info from '$lib/components/icons/Info.svelte';

	export let content = '';
	export let error: any = null;

	const dispatch = createEventDispatcher();
	$: message = error?.message ?? content;
	$: stage = error?.stage ? String(error.stage).replaceAll('_', ' ') : null;
</script>

<div
	class="flex my-2 gap-2.5 border px-4 py-3 border-red-600/10 bg-red-600/10 rounded-lg"
	role="alert"
>
	<div class=" self-start mt-0.5">
		<Info className="size-5 text-red-700 dark:text-red-400" />
	</div>

	<div class="self-center text-sm min-w-0 flex-1">
		{#if stage}
			<div class="font-medium capitalize">{stage}</div>
		{/if}
		<div>{typeof message === 'string' ? message : JSON.stringify(message)}</div>

		{#if error?.operation_id}
			<details class="mt-2 text-xs opacity-75">
				<summary class="cursor-pointer select-none">Technical details</summary>
				<div class="mt-1 break-all font-mono">Correlation ID: {error.operation_id}</div>
			</details>
		{/if}

		{#if error?.retryable}
			<button
				type="button"
				class="mt-2 rounded-lg border border-red-700/20 px-2.5 py-1 font-medium hover:bg-red-700/10 focus:outline-hidden focus:ring-2 focus:ring-red-500/50"
				on:click={() => dispatch('retry')}
			>
				Retry
			</button>
		{/if}
	</div>
</div>

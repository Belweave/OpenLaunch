<script lang="ts">
	import { OPENLAUNCH_BASE_URL } from '$lib/constants';
	import { safeImageUrl } from '$lib/utils/safeImageUrl';

	export let className = 'size-8';
	export let src = `${OPENLAUNCH_BASE_URL}/static/favicon.png`;
</script>

<img
	aria-hidden="true"
	src={safeImageUrl(src)}
	class=" {className} object-cover rounded-full"
	alt="profile"
	draggable="false"
/>

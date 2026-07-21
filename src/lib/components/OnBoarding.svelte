<script>
	import { getContext, onMount } from 'svelte';
	const i18n = getContext('i18n');

	import { OPENLAUNCH_BASE_URL } from '$lib/constants';

	import ArrowRightCircle from './icons/ArrowRightCircle.svelte';

	export let show = true;
	export let getStartedHandler = () => {};

	function setLogoImage() {
		const logo = document.getElementById('logo');

		if (logo) {
			const isDarkMode = document.documentElement.classList.contains('dark');

			if (isDarkMode) {
				const darkImage = new Image();
				darkImage.src = `${OPENLAUNCH_BASE_URL}/static/favicon-dark.png`;

				darkImage.onload = () => {
					logo.src = `${OPENLAUNCH_BASE_URL}/static/favicon-dark.png`;
					logo.style.filter = ''; // Ensure no inversion is applied if splash-dark.png exists
				};

				darkImage.onerror = () => {
					logo.style.filter = 'invert(1)'; // Invert image if splash-dark.png is missing
				};
			}
		}
	}

	$: if (show) {
		setLogoImage();
	}
</script>

{#if show}
	<div class="w-full h-screen max-h-[100dvh] text-white relative">
		<div class="fixed m-10 z-50">
			<div class="flex space-x-2">
				<div class=" self-center">
					<img
						id="logo"
						crossorigin="anonymous"
						src="{OPENLAUNCH_BASE_URL}/static/favicon.png"
						class=" w-6 rounded-full"
						alt="logo"
					/>
				</div>
			</div>
		</div>

		<div
			class="w-full h-full absolute top-0 left-0 bg-cover bg-center"
			style="background-image: url('{OPENLAUNCH_BASE_URL}/assets/images/galaxy.jpg')"
		></div>

		<div
			class="w-full h-full absolute top-0 left-0 bg-linear-to-t from-20% from-black to-transparent"
		></div>

		<div class="w-full h-full absolute top-0 left-0 backdrop-blur-xs bg-black/50"></div>

		<div class="relative bg-transparent w-full h-screen max-h-[100dvh] flex z-10">
			<div class="flex flex-col justify-end w-full items-center pb-10 text-center">
				<div class="text-5xl lg:text-7xl font-secondary">
					<div>{$i18n.t('Explore the cosmos')}</div>
					<div class="mt-0.5">{$i18n.t(`wherever you are`)}</div>
				</div>

				<div class="flex justify-center mt-8">
					<div class="flex flex-col justify-center items-center">
						<button
							aria-label={$i18n.t('Get started')}
							class="relative z-20 flex p-1 rounded-full bg-white/5 hover:bg-white/10 transition font-medium text-sm"
							on:click={() => {
								getStartedHandler();
							}}
						>
							<ArrowRightCircle className="size-6" aria-hidden="true" />
						</button>
						<div class="mt-1.5 font-primary text-base font-medium" aria-hidden="true">
							{$i18n.t(`Get started`)}
						</div>
					</div>
				</div>
			</div>
		</div>
	</div>
{/if}

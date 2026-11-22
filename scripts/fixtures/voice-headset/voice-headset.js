function showVoiceHeadset(providers, draftHasContent, voiceActivity) {
  const showVoiceStart = computed(() => !draftHasContent.value && !voiceActivity.value)
  return showVoiceStart.value
}

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_MODEL_RUNTIME,
  modelReasoningEffortOptions,
  modelRuntimeForm,
  modelRuntimePayload
} from '../src/features/providers/model-runtime'

describe('model runtime configuration', () => {
  it('uses safe defaults for a new model', () => {
    expect(modelRuntimeForm()).toEqual(DEFAULT_MODEL_RUNTIME)
  })

  it('uses only the reasoning efforts advertised by the model', () => {
    expect(modelReasoningEffortOptions({
      reasoningEffort: 'medium',
      reasoningEfforts: ['none', 'low', 'medium', 'xhigh']
    })).toEqual([
      { label: 'Modellstandard (Mittel)', value: null },
      { label: 'Aus', value: 'none' },
      { label: 'Niedrig', value: 'low' },
      { label: 'Mittel', value: 'medium' },
      { label: 'Sehr hoch', value: 'xhigh' }
    ])
    expect(modelReasoningEffortOptions({
      reasoningEffort: 'low',
      reasoningEfforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
    })).toEqual([
      { label: 'Modellstandard (Niedrig)', value: null },
      { label: 'Niedrig', value: 'low' },
      { label: 'Mittel', value: 'medium' },
      { label: 'Hoch', value: 'high' },
      { label: 'Sehr hoch', value: 'xhigh' },
      { label: 'Maximum', value: 'max' },
      { label: 'Ultra', value: 'ultra' }
    ])
    expect(modelReasoningEffortOptions({ reasoningEffort: null })).toEqual([])
  })

  it('reads camelCase API fields and preserves false capabilities', () => {
    expect(modelRuntimeForm({
      capabilities: ['chat'],
      inputModalities: ['text', 'image'],
      contextWindowTokens: 64000,
      discoveredContextWindowTokens: 32000,
      useDiscoveredContextWindow: false,
      contextWindowSource: 'override',
      maxOutputTokens: 8000,
      discoveredMaxOutputTokens: 32768,
      charactersPerToken: 3.5,
      supportsTools: false,
      supportsParallelToolCalls: false,
      supportsStreaming: false,
      supportsImageInput: true,
      trustLevel: null,
      pseudonymizationEnabled: false,
      memoryPolicy: 'deny',
      reasoningSummary: false,
      reasoningEffort: 'low'
    })).toEqual({
      capabilities: ['chat'],
      inputModalities: ['text', 'image'],
      contextWindowTokens: 64000,
      discoveredContextWindowTokens: 32000,
      useDiscoveredContextWindow: false,
      contextWindowSource: 'override',
      maxOutputTokens: 8000,
      discoveredMaxOutputTokens: 32768,
      charactersPerToken: 3.5,
      supportsTools: false,
      supportsParallelToolCalls: false,
      supportsStreaming: false,
      supportsImageInput: true,
      trustLevel: null,
      pseudonymizationEnabled: false,
      memoryPolicy: 'deny',
      reasoningSummary: false,
      reasoningEffort: 'low',
      scriptedToolsEnabled: false
    })
  })

  it('maps form values to additive provider request fields', () => {
    expect(modelRuntimePayload({
      capabilities: ['chat', 'image_generation'],
      inputModalities: ['text', 'image', 'audio', 'video'],
      contextWindowTokens: '32000',
      useDiscoveredContextWindow: true,
      maxOutputTokens: '4096',
      charactersPerToken: '4.2',
      supportsTools: false,
      supportsParallelToolCalls: true,
      supportsStreaming: true,
      supportsImageInput: true,
      trustLevel: 'private',
      pseudonymizationEnabled: null,
      memoryPolicy: 'allow',
      reasoningSummary: false,
      reasoningEffort: 'high'
    })).toEqual({
      capabilities: ['chat', 'image_generation'],
      input_modalities: ['text', 'image', 'audio', 'video'],
      context_window_tokens: 32000,
      use_discovered_context_window: true,
      max_output_tokens: 4096,
      characters_per_token: 4.2,
      supports_tools: false,
      supports_parallel_tool_calls: false,
      scripted_tools_enabled: false,
      supports_streaming: true,
      supports_image_input: true,
      trust_level: 'private',
      pseudonymization_enabled: null,
      memory_policy: 'allow',
      reasoning_summary: false,
      reasoning_effort: 'high'
    })
  })

  it('roundtrips Auto and preserves explicit null over legacy aliases', () => {
    for (const field of ['maxOutputTokens', 'max_output_tokens']) {
      const form = modelRuntimeForm({
        [field]: null,
        config: { maxOutputTokens: 512, max_output_tokens: 1024 },
        discoveredMaxOutputTokens: 32768
      })
      expect(form.maxOutputTokens).toBeNull()
      const payload = modelRuntimePayload(form)
      expect(payload.max_output_tokens).toBeNull()
      expect(payload).not.toHaveProperty('discovered_max_output_tokens')
      expect(payload).not.toHaveProperty('discoveredMaxOutputTokens')
    }
    expect(modelRuntimePayload(modelRuntimeForm()).max_output_tokens).toBeNull()
    expect(modelRuntimeForm({ config: { max_output_tokens: 512 } }).maxOutputTokens).toBe(512)
  })

  it('does not clip manual ceilings to context or provider limits', () => {
    const form = modelRuntimeForm({ maxOutputTokens: 64000, discoveredMaxOutputTokens: 8192 })
    for (const context of [4096, 128000]) {
      form.contextWindowTokens = context
      expect(modelRuntimePayload(form).max_output_tokens).toBe(64000)
    }
  })

  it('preserves inherited model policy values', () => {
    expect(modelRuntimeForm({
      trustLevel: null,
      pseudonymizationEnabled: null
    })).toMatchObject({
      trustLevel: null,
      pseudonymizationEnabled: null
    })
  })

  it('preserves media capabilities without inferring them from image input support', () => {
    expect(modelRuntimeForm({
      capabilities: ['audio_transcription'],
      supportsImageInput: true
    })).toMatchObject({
      capabilities: ['audio_transcription'],
      inputModalities: ['text', 'image'],
      supportsImageInput: true
    })

    expect(modelRuntimePayload({
      ...modelRuntimeForm(),
      capabilities: ['speech_generation'],
      supportsImageInput: false
    })).toMatchObject({
      capabilities: ['speech_generation'],
      supports_image_input: false
    })

    expect(modelRuntimeForm({ capabilities: [] }).capabilities).toEqual([])
    expect(modelRuntimePayload({
      ...modelRuntimeForm(),
      capabilities: []
    }).capabilities).toEqual([])
  })

  it('uses canonical input modalities while keeping the legacy image API field', () => {
    expect(modelRuntimeForm({
      inputModalities: ['video', 'image', 'invalid', 'video'],
      supportsImageInput: false
    })).toMatchObject({
      inputModalities: ['video', 'image'],
      supportsImageInput: true
    })
    expect(modelRuntimePayload({
      ...modelRuntimeForm(),
      inputModalities: ['text', 'audio', 'video'],
      supportsImageInput: true
    }).supports_image_input).toBe(false)
    expect(modelRuntimePayload({
      ...modelRuntimeForm(),
      inputModalities: undefined,
      supportsImageInput: true
    }).supports_image_input).toBe(true)
  })
})

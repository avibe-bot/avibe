// A message spoken rather than typed: the one marker every voice surface sends (a hold
// to talk, the desktop pet's dictation sent as heard). The server keeps it on the
// message, the Agent reads it as speech recognition (`core/agent_input.is_voice_input`),
// and the conversation marks it (VoiceMark).

export type VoiceMessageMetadata = { input: 'voice' };

export const VOICE_MESSAGE_METADATA: VoiceMessageMetadata = { input: 'voice' };

export const isVoiceMessage = (metadata?: Record<string, unknown> | null): boolean => metadata?.input === 'voice';

// A message spoken rather than typed. The hold-to-talk surfaces send it with this
// metadata, the server keeps it on the message, and the conversation marks it
// (VoiceMark) so the reader knows the words came from speech recognition.

export type VoiceMessageMetadata = { input: 'voice' };

export const VOICE_MESSAGE_METADATA: VoiceMessageMetadata = { input: 'voice' };

export const isVoiceMessage = (metadata?: Record<string, unknown> | null): boolean => metadata?.input === 'voice';

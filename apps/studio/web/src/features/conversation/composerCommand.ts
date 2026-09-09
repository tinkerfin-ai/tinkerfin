export type ComposerSubmission =
  | { kind: 'message'; content: string }
  | { kind: 'plan-enable' }
  | { kind: 'plan-message'; content: string }
  | { kind: 'plan-off-unsupported' }

export const parseComposerSubmission = (value: string): ComposerSubmission => {
  const input = value.trim()
  if (input === '/plan') return { kind: 'plan-enable' }
  if (!input.startsWith('/plan') || !/\s/.test(input[5] ?? '')) {
    return { kind: 'message', content: input }
  }
  const content = input.slice(5).trim()
  if (content === 'off') return { kind: 'plan-off-unsupported' }
  return content
    ? { kind: 'plan-message', content }
    : { kind: 'plan-enable' }
}

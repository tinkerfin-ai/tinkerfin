import { describe, expect, it, vi } from 'vitest'

import { TaskTraceFollowOwnership } from './followOwnership'

const waitForAbort = (
  events: string[],
  label: string,
  options: { includeTaskTrace: boolean; signal: AbortSignal },
) => new Promise<void>((resolve) => {
  events.push(`${label}:start:${options.includeTaskTrace}`)
  const finish = () => {
    events.push(`${label}:settled`)
    resolve()
  }
  if (options.signal.aborted) finish()
  else options.signal.addEventListener('abort', finish, { once: true })
})

describe('TaskTraceFollowOwnership', () => {
  it('settles the old true follow before promoting the next thread', async () => {
    const ownership = new TaskTraceFollowOwnership()
    const events: string[] = []
    await ownership.handoff('thread-a')
    const first = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'a', options),
    )
    await Promise.resolve()

    const handoff = await ownership.handoff('thread-b')
    expect(handoff.demotedThreadIds).toEqual(['thread-a'])
    expect(events).toEqual(['a:start:true', 'a:settled'])
    await first

    const background = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'a-background', options),
    )
    const current = ownership.follow(
      'thread-b',
      (options) => waitForAbort(events, 'b', options),
    )
    await vi.waitFor(() => {
      expect(events).toContain('a-background:start:false')
      expect(events).toContain('b:start:true')
    })

    await ownership.close()
    await Promise.all([background, current])
    expect(events.slice(-2).sort()).toEqual(['a-background:settled', 'b:settled'])
  })

  it('serializes rapid handoffs and leaves only the final owner eligible', async () => {
    const ownership = new TaskTraceFollowOwnership()

    const results = await Promise.all([
      ownership.handoff('thread-a'),
      ownership.handoff('thread-b'),
      ownership.handoff('thread-c'),
    ])
    const events: string[] = []
    const current = ownership.follow(
      'thread-c',
      (options) => waitForAbort(events, 'current', options),
    )
    const background = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'background', options),
    )
    await vi.waitFor(() => {
      expect(events).toContain('current:start:true')
      expect(events).toContain('background:start:false')
    })

    expect(results.map((result) => result.epoch)).toEqual([1, 2, 3])
    await ownership.close()
    await Promise.all([current, background])
  })
})
